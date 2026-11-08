#include "runtime.hpp"

#include <boost/asio/steady_timer.hpp>
#include <boost/asio/this_coro.hpp>
#include <boost/asio/use_awaitable.hpp>

#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <sys/wait.h>
#include <unistd.h>

#include <algorithm>
#include <charconv>
#include <chrono>
#include <climits>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <sstream>
#include <system_error>

extern char** environ;

namespace mavi {
namespace {
using namespace std::chrono_literals;
using net::use_awaitable;
using Clock = std::chrono::steady_clock;

struct Child {
    pid_t pid = 0;
    int read_fd = -1;
    ~Child() {
        if (read_fd >= 0) close(read_fd);
        if (pid > 0) {
            kill(pid, SIGKILL);
            while (waitpid(pid, nullptr, 0) < 0 && errno == EINTR) {}
        }
    }
};

Task<std::string> command(std::vector<std::string> args, int seconds = 12) {
    int pipe[2];
    if (pipe2(pipe, O_CLOEXEC) < 0) throw std::system_error(errno, std::generic_category());
    Child child;
    child.read_fd = pipe[0];
    posix_spawn_file_actions_t actions;
    auto code = posix_spawn_file_actions_init(&actions);
    if (code == 0) {
        code = posix_spawn_file_actions_adddup2(&actions, pipe[1], STDOUT_FILENO);
        if (!code) code = posix_spawn_file_actions_adddup2(&actions, pipe[1], STDERR_FILENO);
        if (!code) code = posix_spawn_file_actions_addclosefrom_np(&actions, 3);
        std::vector<char*> argv;
        for (auto& arg : args) argv.push_back(arg.data());
        argv.push_back(nullptr);
        if (!code) code = posix_spawnp(&child.pid, argv[0], &actions, nullptr, argv.data(), environ);
        posix_spawn_file_actions_destroy(&actions);
    }
    close(pipe[1]);
    if (code) throw std::system_error(code, std::generic_category());
    if (fcntl(child.read_fd, F_SETFL, O_NONBLOCK) < 0)
        throw std::system_error(errno, std::generic_category());
    auto until = Clock::now() + std::chrono::seconds(seconds);
    net::steady_timer timer(co_await net::this_coro::executor);
    std::string output;
    std::array<char, 4096> buffer{};
    for (;;) {
        for (;;) {
            auto size = read(child.read_fd, buffer.data(), buffer.size());
            if (size <= 0) break;
            output.append(buffer.data(), size);
            if (output.size() > 1024 * 1024) throw std::runtime_error("系统命令输出超过上限。");
        }
        int status = 0;
        auto finished = waitpid(child.pid, &status, WNOHANG);
        if (finished == child.pid) {
            child.pid = 0;
            // Reap first, then collect bytes written immediately before exit.
            for (ssize_t size; (size = read(child.read_fd, buffer.data(), buffer.size())) > 0;)
                output.append(buffer.data(), size);
            if (WIFEXITED(status) && WEXITSTATUS(status) == 0) co_return output;
            throw std::runtime_error(output.empty() ? "系统操作失败。" : output.substr(0, 300));
        }
        if (finished < 0 && errno != EINTR) throw std::system_error(errno, std::generic_category());
        if (Clock::now() >= until) throw std::runtime_error("系统操作超时，请查看运行日志。");
        timer.expires_after(25ms);
        co_await timer.async_wait(use_awaitable);
    }
}

std::string trim(std::string_view value) {
    auto first = value.find_first_not_of(" \t\r\n");
    if (first == value.npos) return {};
    return std::string(value.substr(first, value.find_last_not_of(" \t\r\n") - first + 1));
}

double numeric(std::string_view value) {
    double number = 0;
    auto [end, ec] = std::from_chars(value.data(), value.data() + value.size(), number);
    if (ec != std::errc() || end != value.data() + value.size() || !std::isfinite(number)) return 0;
    return number;
}

struct Metrics {
    Json values = Json::object();
    int kv_tokens = 0;
};

Metrics parse_metrics(const std::string& text) {
    Metrics metrics;
    std::istringstream input(text);
    std::string line;
    while (std::getline(input, line)) {
        if (line.empty() || line.front() == '#') continue;
        auto end = line.find_first_of(" \t{");
        if (end == line.npos) continue;
        auto name = line.substr(0, end);
        auto start = end;
        if (line[end] == '{') {
            auto labels_end = line.rfind('}');
            if (labels_end == line.npos) continue;
            start = labels_end + 1;
            if (name == "vllm:cache_config_info") {
                constexpr std::string_view key = "kv_cache_size_tokens=\"";
                auto position = line.find(key, end);
                if (position != line.npos) {
                    position += key.size();
                    auto token = std::string_view(line).substr(position, line.find('"', position) - position);
                    auto count = numeric(token);
                    if (count > 0 && count < INT_MAX) metrics.kv_tokens = count;
                }
            }
        }
        start = line.find_first_not_of(" \t", start);
        if (start == line.npos) continue;
        auto value = std::string_view(line).substr(start, line.find_first_of(" \t\r", start) - start);
        metrics.values[name] = metrics.values.value(name, 0.0) + numeric(value);
    }
    return metrics;
}

void system_snapshot(Json& snapshot, uint64_t& previous_total, uint64_t& previous_idle) {
    std::ifstream stat("/proc/stat");
    std::string name;
    stat >> name;
    uint64_t total = 0, idle = 0;
    for (int i = 0; i < 8; ++i) {
        uint64_t value = 0;
        stat >> value;
        total += value;
        if (i == 3 || i == 4) idle += value;
    }
    if (previous_total && total > previous_total && idle >= previous_idle)
        snapshot["cpu"] = std::clamp(100.0 * (1.0 - double(idle - previous_idle) / (total - previous_total)), 0.0, 100.0);
    previous_total = total;
    previous_idle = idle;
    std::ifstream mem("/proc/meminfo");
    std::string line;
    double capacity = 0, available = 0;
    while (std::getline(mem, line)) {
        if (line.starts_with("MemTotal:")) capacity = std::strtod(line.c_str() + 9, nullptr);
        if (line.starts_with("MemAvailable:")) available = std::strtod(line.c_str() + 13, nullptr);
    }
    snapshot["ram_total"] = capacity / (1 << 20);
    snapshot["ram_used"] = std::max(0.0, capacity - available) / (1 << 20);
    std::error_code ec;
    auto disk = std::filesystem::space("/", ec);
    if (!ec) snapshot["disk_free"] = double(disk.free) / (1 << 30);
}

void gpu_snapshot(Json& snapshot, const std::string& output) {
    std::istringstream csv(output.substr(0, output.find('\n')));
    std::array<std::string, 7> values;
    for (auto& value : values)
        if (!std::getline(csv, value, ',')) return;
    snapshot["gpu_name"] = trim(values[0]);
    snapshot["driver"] = trim(values[1]);
    std::array fields{"gpu_used", "gpu_total", "gpu_util", "gpu_temp", "gpu_power"};
    for (size_t i = 0; i < fields.size(); ++i) snapshot[fields[i]] = numeric(trim(values[i + 2]));
}
}

Task<> Runtime::monitor() {
    auto executor = co_await net::this_coro::executor;
    net::steady_timer wait(executor);
    Json previous = Json::object();
    double previous_time = 0;
    uint64_t previous_total = 0, previous_idle = 0;
    const std::vector<std::string> gpu_args = {
        env("MAVI_NVIDIA_SMI", "nvidia-smi"),
        "--query-gpu=name,driver_version,memory.used,memory.total,utilization.gpu,temperature.gpu,power.draw",
        "--format=csv,noheader,nounits"};
    const std::vector<std::string> status_args = {
        env("MAVI_SYSTEMCTL", "/usr/bin/systemctl"), "show", std::string(engine_unit), "-p", "ActiveState",
        "-p", "SubState", "-p", "NRestarts", "-p", "ActiveEnterTimestampMonotonic"};
    for (;;) {
        auto started = Clock::now();
        auto generation = transition_id_;
        auto tick = now();
        Json snapshot = {{"created", tick}, {"gpu_used", 0}, {"gpu_total", 0}, {"gpu_util", 0},
                         {"gpu_temp", 0}, {"gpu_power", 0}, {"cpu", 0}, {"ram_used", 0}, {"ram_total", 0},
                         {"disk_free", 0}, {"output_tps", 0}, {"input_tps", 0}, {"running", 0},
                         {"waiting", 0}, {"kv_usage", 0}, {"gpu_name", "GPU 状态暂不可用"}};
        system_snapshot(snapshot, previous_total, previous_idle);
        try {
            gpu_snapshot(snapshot, co_await command(gpu_args, 8));
        } catch (const std::exception&) {}
        Json service = {{"ActiveState", "unknown"}};
        try {
            std::istringstream input(co_await command(status_args));
            std::string line;
            while (std::getline(input, line)) {
                auto eq = line.find('=');
                if (eq != line.npos) service[line.substr(0, eq)] = line.substr(eq + 1);
            }
        } catch (const std::exception&) {}
        auto health_started = Clock::now();
        Json health_status, health_error;
        bool healthy = false;
        for (int attempt = 0; attempt < 2; ++attempt) {
            try {
                Upstream upstream(executor);
                co_await upstream.open(upstream_port_, http::verb::get, "/health", "", "", 2);
                health_status = upstream.status();
                healthy = upstream.status() == 200;
                health_error = nullptr;
                break;
            } catch (const boost::system::system_error& error) {
                health_error = error.code().message();
            }
            if (!attempt) {
                wait.expires_after(50ms);
                co_await wait.async_wait(use_awaitable);
            }
        }
        snapshot["health_ms"] = std::chrono::duration<double, std::milli>(Clock::now() - health_started).count();
        snapshot["health_ok"] = healthy;
        snapshot["health_status"] = health_status;
        snapshot["health_error"] = health_error;
        Metrics metrics;
        bool fresh = false;
        if (healthy) {
            try {
                Upstream upstream(executor);
                co_await upstream.open(upstream_port_, http::verb::get, "/metrics", "", "", 2);
                if (upstream.status() == 200) {
                    metrics = parse_metrics(co_await upstream.read_all(2 * 1024 * 1024));
                    fresh = true;
                }
            } catch (const std::exception&) {}
            if (!fresh) metrics.values = cache_["metrics"];
        }
        auto active = service.value("ActiveState", "");
        const char* state = healthy ? "running" : active == "deactivating" ? "stopping" :
            (active == "active" || active == "activating") ? "loading" : active == "failed" ? "error" : "stopped";
        if (healthy) {
            snapshot["running"] = metrics.values.value("vllm:num_requests_running", 0.0);
            snapshot["waiting"] = metrics.values.value("vllm:num_requests_waiting", 0.0);
            snapshot["kv_usage"] = metrics.values.value("vllm:kv_cache_usage_perc", 0.0);
            if (fresh && !previous.empty()) {
                const std::pair<const char*, const char*> rates[] = {
                    {"vllm:generation_tokens_total", "output_tps"}, {"vllm:prompt_tokens_total", "input_tps"}};
                for (const auto& [key, field] : rates) {
                    auto current = metrics.values.value(key, 0.0), old = previous.value(key, 0.0);
                    snapshot[field] = std::max(0.0, current >= old ? current - old : current) / std::max(1.0, tick - previous_time);
                }
            }
            if (fresh) {
                previous = metrics.values;
                previous_time = tick;
            }
        } else previous = Json::object();
        auto draft = metrics.values.value("vllm:spec_decode_num_draft_tokens_total", 0.0);
        snapshot["mtp_acceptance"] = draft > 0 ?
            Json(metrics.values.value("vllm:spec_decode_num_accepted_tokens_total", 0.0) / draft) : Json();
        if (generation == transition_id_) {
            cache_ = {{"state", state}, {"hardware", snapshot}, {"metrics", std::move(metrics.values)},
                      {"service", service}, {"updated", tick}, {"mtp_acceptance", snapshot["mtp_acceptance"]},
                      {"kv_tokens", metrics.kv_tokens ? metrics.kv_tokens : healthy ? cache_.value("kv_tokens", 0) : 0}};
            try {
                store_.transaction([&] {
                    if (metrics.kv_tokens) {
                        auto config = store_.applied(store_.config());
                        store_.calibrate(Json{{"kv_tokens", metrics.kv_tokens}, {"concurrency", config.at("concurrency")},
                            {"context_length", config.at("context_length")}, {"measured_at", tick},
                            {"source", "当前配置启动后实测 · 视觉 + MTP"}});
                    }
                    store_.execute("INSERT OR REPLACE INTO samples VALUES (?,?)", {tick, snapshot.dump()});
                    store_.execute("DELETE FROM samples WHERE created<?", {tick - 7 * 86400});
                    store_.execute("DELETE FROM sessions WHERE expires<?", {tick});
                    store_.execute("DELETE FROM login_attempts WHERE created<?", {tick - 900});
                });
            } catch (const std::exception&) {
                std::cerr << "Failed to record runtime sample\n";
            }
        }
        wait.expires_at(std::max(Clock::now() + 500ms, started + 5s));
        co_await wait.async_wait(use_awaitable);
    }
}

Task<> Runtime::control(std::string action) {
    if (action != "start" && action != "stop" && action != "restart" && action != "enable" && action != "disable")
        throw std::invalid_argument("无效操作。");
    auto systemctl = env("MAVI_SYSTEMCTL", "/usr/bin/systemctl");
    auto sudo = env("MAVI_SUDO", "/usr/bin/sudo");
    auto args = [&](const std::string& action) {
        std::vector<std::string> command;
        if (!sudo.empty()) command = {sudo, "-n"};
        command.insert(command.end(), {systemctl, action, std::string(engine_unit), "--no-block"});
        return command;
    };
    if (action == "start" || action == "restart") co_await command(args("reset-failed"));
    co_await command(args(action));
    store_.audit("inference." + action);
}

void Runtime::transition(std::string_view state) {
    ++transition_id_;
    cache_["state"] = state;
}

Task<std::string> Runtime::logs() {
    std::vector<std::string> args = {"/usr/bin/journalctl", "-u", std::string(engine_unit),
                                     "-n", "120", "--no-pager", "-o", "short-iso"};
    std::string result;
    try {
        result = co_await command(std::move(args));
    } catch (const std::exception& error) {
        result = error.what();
    }
    const std::pair<std::string_view, std::string_view> names[] = {
        {"Qwen3.8-27B-UD-Q4_K_M.gguf", "liuxing-UD-Q4_K_M.gguf"}, {"mtp-Qwen3.8-27B-Q4_0.gguf", "mtp-liuxing-Q4_0.gguf"},
        {"mmproj-BF16.gguf", "mmproj-liuxing-BF16.gguf"}, {"qwen3.8-27b", model}};
    for (const auto& [old, current] : names) {
        size_t start = 0;
        while ((start = result.find(old, start)) != result.npos) {
            result.replace(start, old.size(), current);
            start += current.size();
        }
    }
    co_return result;
}

[[noreturn]] void launch_engine(Store& store) {
    auto config = store.config();
    auto context = config.at("context_length").get<int>();
    auto concurrency = config.at("concurrency").get<int>();
    auto pixels = image_max_pixels(context);
    store.set_meta("applied_config", Json{{"concurrency", concurrency}, {"context_length", context},
                                          {"image_max_pixels", pixels}});
    auto home = std::filesystem::path(env("MAVI_INFERENCE_ROOT", "/home/mavi/inference"));
    auto bin = home / "venv-vllm/bin";
    auto cuda = home / "venv-vllm/lib/python3.12/site-packages/nvidia/cu13";
    auto dir = home / "models" / model;
    setenv("HOME", "/home/mavi", 1);
    setenv("CUDA_HOME", cuda.c_str(), 1);
    setenv("HF_HUB_OFFLINE", "1", 1);
    setenv("TRANSFORMERS_OFFLINE", "1", 1);
    setenv("OMP_NUM_THREADS", "1", 1);
    setenv("VLLM_USE_V2_MODEL_RUNNER", "0", 1);
    setenv("VLLM_API_KEY", store.internal_key().c_str(), 1);
    setenv("PYTORCH_ALLOC_CONF", "garbage_collection_threshold:0.8", 1);
    setenv("PATH", (bin.string() + ":" + (cuda / "bin").string() + ":/usr/local/bin:/usr/bin:/bin").c_str(), 1);
    auto image_size = Json{{"size", {{"longest_edge", pixels}, {"shortest_edge", 65536}}}}.dump();
    auto projection = Json{{"mm_proj", (dir / "mmproj-liuxing-BF16.gguf").string()}}.dump();
    std::vector<std::string> args = {
        (bin / "vllm").string(), "serve", (dir / "liuxing-UD-Q4_K_M.gguf").string(),
        "--tokenizer", (dir / "hf-config").string(), "--hf-config-path", (dir / "hf-config").string(),
        "--load-format", "gguf", "--quantization", "gguf", "--dtype", "bfloat16",
        "--reasoning-parser", "qwen3", "--enable-auto-tool-choice", "--tool-call-parser", "qwen3_coder",
        "--served-model-name", std::string(model), "--host", "127.0.0.1", "--port",
        std::to_string(port_env("MAVI_UPSTREAM_PORT", 8000)), "--max-model-len", std::to_string(context),
        "--max-num-seqs", std::to_string(concurrency), "--max-num-batched-tokens", "1024",
        "--gpu-memory-utilization", "0.90", "--enforce-eager", "--default-chat-template-kwargs",
        R"({"enable_thinking":false})", "--no-enable-prefix-caching", "--limit-mm-per-prompt", R"({"image":1,"video":0})",
        "--mm-processor-kwargs", image_size, "--model-loader-extra-config", projection,
        "--speculative-config", R"({"method":"mtp","num_speculative_tokens":1})"};
    std::vector<char*> argv;
    for (auto& arg : args) argv.push_back(arg.data());
    argv.push_back(nullptr);
    execv(argv[0], argv.data());
    throw std::system_error(errno, std::generic_category(), "Cannot start vLLM");
}
}
