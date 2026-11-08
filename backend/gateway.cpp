#include "gateway.hpp"
#include "crypto.hpp"

#include <chrono>
#include <iostream>
#include <optional>

namespace mavi {
namespace {
using Clock = std::chrono::steady_clock;

bool truth(const Json& value) {
    if (value.is_boolean()) return value.get<bool>();
    if (value.is_number()) return value.get<double>() != 0;
    return !value.is_null() && !value.empty() && value != "";
}

void enforce_policy(Json& body, const Json& config, const Json& applied, std::string_view route) {
    if (!body.is_object()) throw HttpError(400, "请求必须是 JSON 对象。");
    if (body.contains("stream") && !body["stream"].is_boolean())
        throw HttpError(400, "stream 必须是布尔值。");
    for (auto field : {"stream_options", "chat_template_kwargs"}) {
        if (body.contains(field) && !body[field].is_null() && !body[field].is_object())
            throw HttpError(400, std::string(field) + " 必须是 JSON 对象。");
    }
    const auto options = body.value("stream_options", Json::object());
    if (options.contains("include_usage") && !options["include_usage"].is_boolean())
        throw HttpError(400, "include_usage 必须是布尔值。");
    for (auto field : {"n", "best_of", "beam_width"}) {
        if (body.contains(field) && (!body[field].is_number_integer() || body[field] != 1))
            throw HttpError(400, std::string("单卡配置仅支持 ") + field + "=1。");
    }
    if (body.contains("use_beam_search") && !body["use_beam_search"].is_null() &&
        body["use_beam_search"] != false && body["use_beam_search"] != 0)
        throw HttpError(400, "单卡稳定配置不支持 beam search。");
    if (route == "completions" && body.contains("prompt") && body["prompt"].is_array())
        for (const auto& item : body["prompt"])
            if (!item.is_number_integer()) throw HttpError(400, "批量 prompt 请拆为独立请求。");
    int limit = std::min(4096, applied.at("context_length").get<int>());
    for (auto field : {"max_tokens", "max_completion_tokens", "max_output_tokens"}) {
        if (body.contains(field) && !body[field].is_null() &&
            (!body[field].is_number_integer() || body[field] < 1 || body[field] > limit))
            throw HttpError(400, std::string(field) + " 必须为 1–" + std::to_string(limit) + " 的整数。");
    }
    for (auto field : {"chat_template", "continue_final_message", "add_generation_prompt"})
        if (body.contains(field)) throw HttpError(400, "此网关不接受请求自定义模板或续写控制参数。");
    if (!config.at("thinking_enabled").get<bool>()) {
        if (route == "completions")
            throw HttpError(400, "强制关闭思考时请使用 /v1/chat/completions 或 /v1/responses。");
        body.erase("thinking");
        body.erase("enable_thinking");
        auto& kwargs = body["chat_template_kwargs"];
        if (kwargs.is_null()) kwargs = Json::object();
        kwargs.erase("reasoning_effort");
        kwargs["enable_thinking"] = false;
        if (route == "responses") body["reasoning"] = Json{{"effort", "none"}};
        else body["reasoning_effort"] = "none";
    }
    body["model"] = model;
}

void rename_model(Json& result, const std::string& alias) {
    if (!result.is_object()) return;
    if (result.contains("model")) result["model"] = alias;
    if (result.contains("response")) rename_model(result["response"], alias);
}

void extract_usage(const Json& result, Json& row) {
    const Json* usage = nullptr;
    if (result.contains("usage") && result["usage"].is_object() && !result["usage"].empty())
        usage = &result["usage"];
    else if (result.contains("response") && result["response"].contains("usage") &&
             result["response"]["usage"].is_object())
        usage = &result["response"]["usage"];
    if (!usage) return;
    auto input = usage->value("prompt_tokens", usage->value("input_tokens", Json()));
    auto output = usage->value("completion_tokens", usage->value("output_tokens", Json()));
    if (input.is_number_integer() && input != 0) row["input_tokens"] = input;
    if (output.is_number_integer() && output != 0) row["output_tokens"] = output;
}

struct RequestLog {
    Store& store;
    Clock::time_point start = Clock::now();
    Json row;
    bool written = false;

    RequestLog(Store& store, const Json& key, std::string_view path, std::string_view alias)
        : store(store), row({{"id", random_hex(16)}, {"created", now()}, {"key_id", key.at("id")},
          {"key_name", key.at("name")}, {"route", path}, {"model", alias}, {"status", 500}, {"outcome", "error"},
          {"latency_ms", 0}, {"ttft_ms", nullptr}, {"input_tokens", 0}, {"output_tokens", 0}, {"stream", 0}}) {}
    ~RequestLog() { finish(); }
    double elapsed() const {
        return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
    }
    void finish() noexcept {
        if (written) return;
        written = true;
        try {
            row["latency_ms"] = elapsed();
            store.record(row);
        } catch (const std::exception&) {
            std::cerr << "Failed to record gateway request\n";
        }
    }
    void fail(int status) {
        row["status"] = status;
        if (status == 499) row["outcome"] = "cancelled";
        finish();
    }
};

struct Admission {
    int& count;
    explicit Admission(int& count) : count(count) { ++count; }
    ~Admission() { --count; }
};

std::string model_key(Connection& conn) {
    auto header = conn.header("Authorization");
    if (header.size() >= 7 && beast::iequals(beast::string_view(header.data(), 7), "bearer "))
        return header.substr(7);
    return conn.header("X-API-Key");
}

bool substantive(const Json& obj, const std::string& kind) {
    if (kind.ends_with(".delta") && obj.contains("delta") && truth(obj["delta"])) return true;
    if (!obj.contains("choices") || !obj["choices"].is_array()) return false;
    for (const auto& choice : obj["choices"]) {
        if (!choice.is_object() || !choice.contains("delta") || !choice["delta"].is_object()) continue;
        const auto& delta = choice["delta"];
        for (auto key : {"content", "reasoning", "reasoning_content", "tool_calls"})
            if (delta.contains(key) && truth(delta[key])) return true;
    }
    return false;
}

// Accumulate one SSE event across upstream HTTP reads, including split CRLF.
class Events {
public:
    void append(std::string_view bytes) { input_.append(bytes); }
    std::optional<std::string> pop() {
        size_t end;
        while ((end = input_.find('\n', offset_)) != std::string::npos) {
            auto length = end - offset_;
            if (length && input_[end - 1] == '\r') --length;
            if (!length) {
                input_.erase(0, end + 1);
                offset_ = 0;
                return std::exchange(event_, {});
            }
            event_.append(input_, offset_, length);
            event_ += '\n';
            offset_ = end + 1;
        }
        input_.erase(0, offset_);
        offset_ = 0;
        if (input_.size() + event_.size() > 1024 * 1024)
            throw std::runtime_error("Oversized SSE event");
        return std::nullopt;
    }
private:
    std::string input_, event_;
    size_t offset_ = 0;
};

std::string rewrite_event(const std::string& event, const std::string& alias, bool hide_usage,
                          RequestLog& log, bool& completed, bool& failed) {
    std::string data, other;
    size_t start = 0;
    while (start < event.size()) {
        auto end = event.find('\n', start);
        auto line = std::string_view(event).substr(start, end - start);
        if (line.starts_with("data:")) {
            if (!data.empty()) data += '\n';
            line.remove_prefix(5);
            if (line.starts_with(' ')) line.remove_prefix(1);
            data += line;
        } else {
            other += line;
            other += '\n';
        }
        start = end + 1;
    }
    if (data == "[DONE]") completed = true;
    if (data.empty() || data == "[DONE]") return event + '\n';
    Json obj;
    try {
        obj = parse_json(data);
    } catch (const std::exception&) {
        return event + '\n';
    }
    if (!obj.is_object()) return event + '\n';
    std::string kind;
    if (obj.contains("type") && obj["type"].is_string()) kind = obj["type"].get<std::string>();
    if (obj.contains("error") || kind == "error" || kind == "response.failed" || kind == "response.incomplete")
        failed = true;
    if (kind == "response.completed") completed = true;
    extract_usage(obj, log.row);
    if (log.row["ttft_ms"].is_null() && substantive(obj, kind)) log.row["ttft_ms"] = log.elapsed();
    if (hide_usage && obj.contains("choices") && obj["choices"] == Json::array() &&
        obj.contains("usage") && truth(obj["usage"])) return {};
    rename_model(obj, alias);
    return other + "data: " + obj.dump() + "\n\n";
}

Task<> stream_response(Connection& conn, Upstream& upstream, RequestLog& log, const std::string& alias,
                       bool hide_usage) {
    bool completed = false, failed = false, interrupted = false;
    Headers headers = {{"Cache-Control", "no-cache"}, {"X-Accel-Buffering", "no"},
                       {"X-Request-Id", log.row["id"].get<std::string>()}};
    try {
        co_await conn.start_stream(upstream.status(), "text/event-stream", std::move(headers));
    } catch (const boost::system::system_error&) {
        log.row["outcome"] = "cancelled";
        co_return;
    }
    Events events;
    while (!upstream.done() && !conn.disconnected()) {
        try {
            events.append(co_await upstream.next());
        } catch (const std::exception&) {
            interrupted = true;
            break;
        }
        try {
            while (auto event = events.pop()) {
                auto output = rewrite_event(*event, alias, hide_usage, log, completed, failed);
                co_await conn.chunk(output);
            }
        } catch (const boost::system::system_error&) {
            log.row["outcome"] = "cancelled";
            co_return;
        } catch (const std::exception&) {
            interrupted = true;
            break;
        }
    }
    if (conn.disconnected()) {
        log.row["outcome"] = "cancelled";
        co_return;
    }
    if (interrupted) {
        try {
            co_await conn.chunk("data: {\"error\":{\"message\":\"Upstream stream interrupted\"}}\n\n");
        } catch (const boost::system::system_error&) {
            log.row["outcome"] = "cancelled";
            co_return;
        }
    }
    log.row["outcome"] = completed && !failed && !interrupted && upstream.status() == 200 ? "success" : "error";
    log.finish();
    co_await conn.end_stream();
}
}

Task<> Gateway::handle(Connection& conn) {
    const auto path = conn.path();
    const auto method = conn.request().method();
    if (method == http::verb::get && path == "/health") {
        auto body = Json{{"gateway", "online"}};
        co_return co_await conn.json(200, std::move(body));
    }
    const bool listing = method == http::verb::get && path == "/v1/models";
    if (!listing && (method != http::verb::post || !path.starts_with("/v1/")))
        co_return co_await conn.json(404, api_error("不支持的 OpenAI API 路径。", 404));
    auto key = store_.lookup_key(model_key(conn));
    if (key.is_null())
        co_return co_await conn.json(401, api_error("API Key 无效或已停用。", 401, "authentication_error"));
    auto config = store_.config();
    auto applied = store_.applied(config);
    auto alias = config.at("served_model_name").get<std::string>();
    if (listing) {
        Json item = {{"id", alias}, {"object", "model"}, {"created", 1793923200}, {"owned_by", "mavi"},
                     {"max_model_len", applied.at("context_length")}};
        Json result = {{"object", "list"}, {"data", Json::array({std::move(item)})}};
        co_return co_await conn.json(200, std::move(result));
    }
    auto route = std::string_view(path).substr(4);
    if (route != "chat/completions" && route != "completions" && route != "responses")
        co_return co_await conn.json(404, api_error("不支持的 OpenAI API 路径。", 404));
    RequestLog log(store_, key, path, alias);
    if (admitted_ >= std::min(16, applied.at("concurrency").get<int>() + 6)) {
        log.fail(429);
        Headers headers = {{"Retry-After", "3"}};
        co_return co_await conn.json(429, api_error("推理队列已满，请稍后重试。", 429, "rate_limit_error"),
                                     std::move(headers));
    }
    Admission slot(admitted_);
    Json body;
    std::optional<HttpError> rejection;
    bool requested_usage = false, streaming = false;
    try {
        body = parse_json(co_await conn.read_body());
        if (!body.is_object()) throw HttpError(400, "请求必须是 JSON 对象。");
        if (body.contains("stream")) log.row["stream"] = int(truth(body["stream"]));
        if (!body.contains("model") || body["model"] != alias)
            throw HttpError(404, "未知模型名称。请使用 " + alias);
        enforce_policy(body, config, applied, route);
        streaming = body.value("stream", false);
        if (streaming && route != "responses") {
            auto& options = body["stream_options"];
            if (options.is_null()) options = Json::object();
            requested_usage = options.value("include_usage", false);
            options["include_usage"] = true;
        }
    } catch (const HttpError& error) {
        rejection = error;
    } catch (const Json::exception&) {
        rejection.emplace(400, "请求必须是有效的 JSON 对象。");
    } catch (const std::invalid_argument&) {
        rejection.emplace(400, "请求必须是有效的 JSON 对象。");
    }
    if (rejection) {
        log.fail(rejection->status);
        if (rejection->status != 499)
            co_await conn.json(rejection->status, api_error(rejection->what(), rejection->status));
        co_return;
    }
    auto request = body.dump();
    body = nullptr;
    Upstream upstream(conn.executor());
    conn.watch_disconnect(upstream.socket());
    bool unavailable = false;
    try {
        co_await upstream.open(upstream_port_, http::verb::post, path, std::move(request), store_.internal_key());
    } catch (const boost::system::system_error&) {
        unavailable = true;
    }
    if (conn.disconnected()) {
        log.fail(499);
        co_return;
    }
    if (unavailable) {
        log.fail(503);
        co_return co_await conn.json(503, api_error("推理服务未就绪或已停止，请在管理平台启动模型。", 503,
                                                    "service_unavailable"));
    }
    log.row["status"] = upstream.status();
    if (streaming && upstream.status() < 400)
        co_return co_await stream_response(conn, upstream, log, alias, !requested_usage && route != "responses");
    try {
        body = parse_json(co_await upstream.read_all());
        rename_model(body, alias);
        extract_usage(body, log.row);
    } catch (const std::exception&) {
        if (conn.disconnected()) {
            log.fail(499);
            co_return;
        }
        unavailable = true;
    }
    if (unavailable) {
        log.fail(502);
        co_return co_await conn.json(502, api_error("上游返回无效响应。", 502, "upstream_error"));
    }
    log.row["outcome"] = upstream.status() < 400 ? "success" : "error";
    log.finish();
    co_await conn.json(upstream.status(), std::move(body));
}
}
