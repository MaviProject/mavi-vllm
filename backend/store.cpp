#include "store.hpp"
#include "crypto.hpp"

#include <sqlite3.h>

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <sstream>
#include <stdexcept>

namespace mavi {
namespace {
const Json defaults = {{"served_model_name", model}, {"concurrency", 2}, {"context_length", 12288},
                       {"thinking_enabled", false}, {"autostart_inference", true}};
constexpr std::array concurrency_levels{1, 2, 4, 8};
constexpr std::array context_limits{24576, 12288, 6144, 1024};
const Json default_calibration = {{"kv_tokens", 35659}, {"concurrency", 1}};

struct Statement {
    sqlite3* db;
    sqlite3_stmt* stmt = nullptr;

    Statement(sqlite3* db, const char* sql, Store::Params params) : db(db) {
        if (sqlite3_prepare_v2(db, sql, -1, &stmt, nullptr) != SQLITE_OK) fail();
        try {
            int i = 0;
            for (const auto& value : params) {
                ++i;
                int rc;
                if (value.is_null()) rc = sqlite3_bind_null(stmt, i);
                else if (value.is_boolean()) rc = sqlite3_bind_int(stmt, i, value.get<bool>());
                else if (value.is_number_integer()) rc = sqlite3_bind_int64(stmt, i, value.get<int64_t>());
                else if (value.is_number_float()) rc = sqlite3_bind_double(stmt, i, value.get<double>());
                else {
                    const auto& text = value.get_ref<const std::string&>();
                    rc = sqlite3_bind_text(stmt, i, text.c_str(), text.size(), SQLITE_TRANSIENT);
                }
                if (rc != SQLITE_OK) fail();
            }
        } catch (...) {
            sqlite3_finalize(stmt);
            throw;
        }
    }
    ~Statement() { sqlite3_finalize(stmt); }
    [[noreturn]] void fail() const { throw std::runtime_error(sqlite3_errmsg(db)); }
    bool next() const {
        auto rc = sqlite3_step(stmt);
        if (rc == SQLITE_ROW) return true;
        if (rc == SQLITE_DONE) return false;
        fail();
    }
    Json row() const {
        Json row = Json::object();
        for (int i = 0; i < sqlite3_column_count(stmt); ++i) {
            auto& value = row[sqlite3_column_name(stmt, i)];
            switch (sqlite3_column_type(stmt, i)) {
                case SQLITE_INTEGER: value = sqlite3_column_int64(stmt, i); break;
                case SQLITE_FLOAT: value = sqlite3_column_double(stmt, i); break;
                case SQLITE_TEXT:
                    value = std::string(reinterpret_cast<const char*>(sqlite3_column_text(stmt, i)),
                                        sqlite3_column_bytes(stmt, i));
                    break;
                default: value = nullptr;
            }
        }
        return row;
    }
};

struct Range {
    int lower, upper;
    double fraction;
};

Range range(int n) {
    auto end = std::lower_bound(concurrency_levels.begin(), concurrency_levels.end(), n);
    if (n < 1 || end == concurrency_levels.end()) throw std::invalid_argument("Invalid concurrency");
    int upper = end - concurrency_levels.begin();
    int lower = *end == n ? upper : upper - 1;
    return {lower, upper, lower == upper ? 0.0 :
        double(n - concurrency_levels[lower]) / (concurrency_levels[upper] - concurrency_levels[lower])};
}

int envelope(int n, Range r) {
    return ((1 - r.fraction) * context_limits[r.lower] * concurrency_levels[r.lower] +
            r.fraction * context_limits[r.upper] * concurrency_levels[r.upper]) / n;
}

Json profile(int n, const Json& calibration, const Json& measured) {
    auto r = range(n);
    auto baseline = std::max(8192, calibration.at("kv_tokens").get<int>() +
                                  (calibration.value("concurrency", 1) - 1) * 1536);
    auto measured_or_estimated = [&](int concurrency) {
        return measured.value(std::to_string(concurrency), Json::object()).value(
            "kv_tokens", std::max(4096, baseline - (concurrency - 1) * 1536));
    };
    auto exact = measured.find(std::to_string(n));
    double capacity = exact != measured.end() ? exact->at("kv_tokens").get<double>() :
        measured_or_estimated(concurrency_levels[r.lower]) * (1 - r.fraction) +
        measured_or_estimated(concurrency_levels[r.upper]) * r.fraction;
    int limit = std::clamp(static_cast<int>(capacity * .85 / n), 1024, envelope(n, r));
    int recommended = std::clamp(static_cast<int>(capacity * .75 / n / 1024) * 1024, 1024, limit);
    return {{"concurrency", n}, {"context_length", recommended}, {"max_context", limit},
            {"estimated", exact == measured.end()}};
}
}

double now() {
    return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count();
}

std::string env(const char* name, std::string fallback) {
    const char* value = std::getenv(name);
    return value ? value : std::move(fallback);
}

std::string read_file(const std::filesystem::path& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("Cannot open " + path.string());
    return {std::istreambuf_iterator<char>(input), {}};
}

Json parse_json(std::string_view input) {
    return Json::parse(input, [](int depth, Json::parse_event_t, Json&) {
        if (depth > 128) throw std::invalid_argument("JSON nesting limit exceeded");
        return true;
    });
}

int image_max_pixels(int context) {
    return std::clamp((context / 4) * 1024, 65536, 1048576);
}

Store::Store(std::filesystem::path root_path) : root(std::move(root_path)), data(root / "data") {
    std::filesystem::create_directories(data);
    auto rc = sqlite3_open_v2((data / "platform.sqlite3").c_str(), &db_,
                              SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_NOMUTEX, nullptr);
    try {
        if (rc != SQLITE_OK) throw std::runtime_error("Cannot open platform.sqlite3");
        sqlite3_busy_timeout(db_, 2000);
        // Concurrent first starts can race on the journal-mode lock upgrade,
        // which may return SQLITE_BUSY without running SQLite's busy handler.
        auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
        for (;;) {
            rc = sqlite3_exec(db_, "PRAGMA journal_mode=WAL", nullptr, nullptr, nullptr);
            if (rc == SQLITE_OK) break;
            if ((rc != SQLITE_BUSY && rc != SQLITE_LOCKED) || std::chrono::steady_clock::now() >= deadline)
                throw std::runtime_error(sqlite3_errmsg(db_));
            sqlite3_sleep(25);
        }
        initialize();
        std::filesystem::permissions(data / "platform.sqlite3",
            std::filesystem::perms::owner_read | std::filesystem::perms::owner_write);
    } catch (...) {
        sqlite3_close(db_);
        throw;
    }
}

Store::~Store() { sqlite3_close(db_); }

void Store::script(const char* sql) {
    if (sqlite3_exec(db_, sql, nullptr, nullptr, nullptr) != SQLITE_OK)
        throw std::runtime_error(sqlite3_errmsg(db_));
}

void Store::transaction(const std::function<void()>& write) {
    script("BEGIN IMMEDIATE");
    try {
        write();
        script("COMMIT");
    } catch (...) {
        sqlite3_exec(db_, "ROLLBACK", nullptr, nullptr, nullptr);
        throw;
    }
}

void Store::execute(const char* sql, Params params) {
    Statement statement(db_, sql, params);
    statement.next();
}

Json Store::query(const char* sql, Params params) {
    Statement statement(db_, sql, params);
    Json result = Json::array();
    while (statement.next()) result.push_back(statement.row());
    return result;
}

Json Store::one(const char* sql, Params params) {
    Statement statement(db_, sql, params);
    return statement.next() ? statement.row() : Json();
}

Json Store::meta(std::string_view key, Json fallback) {
    auto row = one("SELECT value FROM meta WHERE key=?", {key});
    return row.is_null() ? std::move(fallback) : parse_json(row["value"].get_ref<const std::string&>());
}

void Store::set_meta(std::string_view key, const Json& value) {
    execute("INSERT OR REPLACE INTO meta VALUES (?,?)", {key, value.dump()});
}

void Store::initialize() {
    transaction([&] {
        script(R"SQL(
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY,password TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY,csrf TEXT NOT NULL,expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS api_keys (id TEXT PRIMARY KEY,name TEXT NOT NULL,digest TEXT UNIQUE NOT NULL,
              encrypted TEXT NOT NULL,tail TEXT NOT NULL,enabled INTEGER NOT NULL,created REAL NOT NULL,last_used REAL);
            CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY,created REAL NOT NULL,key_id TEXT,key_name TEXT,
              route TEXT,model TEXT,status INTEGER,outcome TEXT,latency_ms REAL,ttft_ms REAL,input_tokens INTEGER,
              output_tokens INTEGER,stream INTEGER);
            CREATE INDEX IF NOT EXISTS requests_created ON requests(created);
            CREATE INDEX IF NOT EXISTS requests_key ON requests(key_id);
            CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY,created REAL,action TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS samples (created REAL PRIMARY KEY,payload TEXT);
            CREATE TABLE IF NOT EXISTS login_attempts (ip TEXT,created REAL);
        )SQL");
        auto key_path = data / "secret.key";
        if (!std::filesystem::exists(key_path) || std::filesystem::file_size(key_path) == 0) {
            if (!meta("internal_key").is_null() || !one("SELECT id FROM api_keys LIMIT 1").is_null())
                throw std::runtime_error("secret.key is required to open the existing database");
            auto staging = data / "secret.key.new";
            std::ofstream output(staging, std::ios::binary);
            output.exceptions(std::ios::badbit | std::ios::failbit);
            output << fernet_key();
            output.close();
            std::filesystem::rename(staging, key_path);
        }
        key_ = read_file(key_path);
        if (meta("config").is_null()) set_meta("config", defaults);
        if (one("SELECT username FROM users LIMIT 1").is_null()) {
            execute("INSERT INTO users VALUES (?,?)", {"admin", password_hash("admin")});
            write_bootstrap();
        }
        auto stored = meta("internal_key");
        if (stored.is_null()) {
            internal_ = "sk-internal-" + random_hex(24);
            set_meta("internal_key", seal(key_, internal_));
        } else internal_ = decrypt(stored.get_ref<const std::string&>());
        if (meta("keys_initialized").is_null()) {
            std::ifstream old(env("MAVI_EXISTING_API_ENV", "/home/mavi/inference/config/api.env"));
            std::string value, line;
            while (std::getline(old, line)) {
                if (line.ends_with('\r')) line.pop_back();
                if (line.starts_with("VLLM_API_KEY=")) value = line.substr(13);
            }
            insert_key("原有 API Key", value.empty() ? "sk-mavi-" + random_hex(24) : value);
            set_meta("keys_initialized", true);
        }
        if (meta("calibration").is_null()) {
            auto calibration = default_calibration;
            calibration["source"] = "该主机视觉 + MTP 启动实测";
            calibration["measured_at"] = now();
            set_meta("calibration", calibration);
        }
    });
}

void Store::write_bootstrap() {
    auto staging = data / "bootstrap-credentials.json.new";
    std::ofstream output(staging);
    output.exceptions(std::ios::badbit | std::ios::failbit);
    output << Json({{"url", "http://127.0.0.1:20204/"},
                    {"remote_url_template", "http://<server-ip>:20204/"},
                    {"username", "admin"}, {"password", "admin"}}).dump(2);
    output.close();
    std::filesystem::rename(staging, data / "bootstrap-credentials.json");
}

Json Store::config() { return meta("config", defaults); }
Json Store::applied(const Json& config) { return meta("applied_config", config); }

std::string Store::decrypt(std::string_view value) { return unseal(key_, value); }

Json Store::lookup_key(std::string_view value) {
    if (value.empty() || std::count_if(value.begin(), value.end(),
            [](unsigned char c) { return (c & 0xc0) != 0x80; }) > 512) return nullptr;
    return one("SELECT id,name FROM api_keys WHERE digest=? AND enabled=1", {digest(value)});
}

std::string Store::insert_key(std::string_view name, std::string_view value) {
    auto id = random_hex(8);
    auto tail = value.size();
    for (int i = 0; i < 4 && tail; ++i) {
        --tail;
        while (tail && (static_cast<unsigned char>(value[tail]) & 0xc0) == 0x80) --tail;
    }
    execute("INSERT INTO api_keys VALUES (?,?,?,?,?,?,?,NULL)",
            {id, name, digest(value), seal(key_, value), value.substr(tail), 1, now()});
    return id;
}

void Store::audit(std::string_view action, std::string_view detail) {
    execute("INSERT INTO audit(created,action,detail) VALUES (?,?,?)", {now(), action, detail});
}

void Store::record(const Json& r) {
    transaction([&] {
        execute("INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                {r.at("id"), r.at("created"), r.at("key_id"), r.at("key_name"), r.at("route"), r.at("model"),
                 r.at("status"), r.at("outcome"), r.at("latency_ms"), r.at("ttft_ms"),
                 r.at("input_tokens"), r.at("output_tokens"), r.at("stream")});
        execute("UPDATE api_keys SET last_used=? WHERE id=?", {now(), r.at("key_id")});
    });
}

Json Store::keys() {
    return query(R"SQL(SELECT k.id,k.name,k.tail,k.enabled,k.created,k.last_used,COUNT(r.id) requests,
        COALESCE(SUM(r.output_tokens),0) output_tokens FROM api_keys k LEFT JOIN requests r
        ON k.id=r.key_id GROUP BY k.id ORDER BY k.created)SQL");
}

Json Store::usage() {
    auto totals = one(R"SQL(SELECT COUNT(*) requests, COALESCE(SUM(outcome='success'),0) success,
        COALESCE(SUM(outcome='error'),0) errors, COALESCE(SUM(outcome='cancelled'),0) cancelled,
        COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(output_tokens),0) output_tokens,
        COALESCE(AVG(latency_ms),0) latency_ms, COALESCE(AVG(ttft_ms),0) ttft_ms FROM requests)SQL");
    totals["today"] = one("SELECT COUNT(*) n FROM requests WHERE created>?", {now() - 86400})["n"];
    auto rows = query("SELECT latency_ms FROM requests ORDER BY created DESC LIMIT 500");
    std::vector<double> latencies;
    for (const auto& row : rows) latencies.push_back(row.at("latency_ms").get<double>());
    std::sort(latencies.begin(), latencies.end());
    totals["p95_ms"] = latencies.empty() ? 0.0 : latencies[static_cast<size_t>(latencies.size() * .95)];
    auto samples = query("SELECT payload FROM samples WHERE created>? ORDER BY created", {now() - 3600});
    Json history = Json::array();
    auto stride = std::max<size_t>(1, (samples.size() + 359) / 360);
    for (size_t i = 0; i < samples.size(); i += stride)
        history.push_back(parse_json(samples[i]["payload"].get_ref<const std::string&>()));
    if (!samples.empty() && (samples.size() - 1) % stride)
        history.push_back(parse_json(samples.back()["payload"].get_ref<const std::string&>()));
    return {{"totals", totals}, {"history", history},
            {"recent", query("SELECT * FROM requests ORDER BY created DESC LIMIT 20")},
            {"keys", keys()},
            {"audit", query("SELECT * FROM audit ORDER BY id DESC LIMIT 30")}};
}

Json Store::capacity(int concurrency) {
    return profile(concurrency, meta("calibration", default_calibration), meta("capacity_profiles", Json::object()));
}

Json Store::recommendations() {
    auto calibration = meta("calibration", default_calibration);
    auto measured = meta("capacity_profiles", Json::object());
    Json anchors = Json::array(), all = Json::array();
    for (int n = 1; n <= 8; ++n) {
        auto p = profile(n, calibration, measured);
        if (std::binary_search(concurrency_levels.begin(), concurrency_levels.end(), n)) anchors.push_back(p);
        all.push_back(std::move(p));
    }
    return {{"profiles", anchors}, {"custom_profiles", all}, {"calibration", calibration},
            {"limits", {{"min_concurrency", 1}, {"max_concurrency", 8}, {"min_context", 1024}, {"max_context", 32768}}},
            {"explanation", "快捷档位参考本机压测，自定义并发参考实测或相邻档位估算。"}};
}

void Store::calibrate(const Json& calibration) {
    int concurrency = calibration.at("concurrency").get<int>();
    set_meta("calibration", calibration);
    if (calibration.at("context_length").get<int>() >= envelope(concurrency, range(concurrency))) {
        auto profiles = meta("capacity_profiles", Json::object());
        profiles[std::to_string(concurrency)] = calibration;
        set_meta("capacity_profiles", profiles);
    }
}
}
