#include "admin.hpp"
#include "crypto.hpp"

#include <boost/asio/steady_timer.hpp>
#include <boost/asio/use_awaitable.hpp>

#include <algorithm>
#include <cctype>
#include <ctime>
#include <optional>

namespace mavi {
namespace {
using namespace std::chrono_literals;
constexpr double session_lifetime = 43200;
constexpr std::string_view cookie_name = "mavi_session=";

struct SessionResponse {
    Json body;
    Headers headers;
};

SessionResponse new_session(Store& store) {
    auto token = session_token(), csrf = session_token();
    store.execute("INSERT INTO sessions VALUES (?,?,?)", {digest(token), csrf, now() + session_lifetime});
    auto user = store.one("SELECT username FROM users LIMIT 1");
    return {{{"username", user["username"]}, {"csrf", csrf}},
            {{"Set-Cookie", std::string(cookie_name) + token + "; Max-Age=43200; HttpOnly; Path=/; SameSite=Strict"}}};
}

std::string session_cookie(const std::string& header) {
    std::string_view list(header);
    while (!list.empty()) {
        auto end = list.find(';');
        auto item = list.substr(0, end);
        auto first = item.find_first_not_of(" \t");
        if (first != item.npos && item.substr(first).starts_with(cookie_name))
            return std::string(item.substr(first + cookie_name.size()));
        if (end == list.npos) break;
        list.remove_prefix(end + 1);
    }
    return {};
}

Task<Json> read_json(Connection& conn) {
    auto raw = co_await conn.read_body(16 * 1024);
    try {
        auto body = parse_json(raw);
        if (!body.is_object()) throw HttpError(422, "请求必须是 JSON 对象。");
        co_return body;
    } catch (const Json::exception&) {
        throw HttpError(422, "请求必须是有效的 JSON 对象。");
    } catch (const std::invalid_argument&) {
        throw HttpError(422, "请求必须是有效的 JSON 对象。");
    }
}

size_t characters(std::string_view value) {
    return std::count_if(value.begin(), value.end(), [](unsigned char c) { return (c & 0xc0) != 0x80; });
}

std::string field_text(const Json& body, const char* field, size_t min, size_t max) {
    if (body.contains(field) && body[field].is_string()) {
        const auto& value = body[field].get_ref<const std::string&>();
        auto length = characters(value);
        if (length >= min && length <= max) return value;
    }
    throw HttpError(422, std::string("无效字段：") + field);
}

int field_int(const Json& body, const char* field, int min, int max) {
    if (!body.contains(field) || !body[field].is_number_integer() || body[field] < min || body[field] > max)
        throw HttpError(422, std::string("无效字段：") + field);
    return body[field].get<int>();
}

bool field_bool(const Json& body, const char* field) {
    if (body.contains(field)) {
        const auto& value = body[field];
        if (value.is_boolean()) return value.get<bool>();
        if (value.is_number() && (value == 0 || value == 1)) return value == 1;
        if (value.is_string()) {
            auto token = value.get<std::string>();
            std::transform(token.begin(), token.end(), token.begin(), [](unsigned char c) { return std::tolower(c); });
            if (token == "true" || token == "1" || token == "on" || token == "yes" || token == "t" || token == "y")
                return true;
            if (token == "false" || token == "0" || token == "off" || token == "no" || token == "f" || token == "n")
                return false;
        }
    }
    throw HttpError(422, std::string("无效字段：") + field);
}

std::string strip(const std::string& value) {
    auto start = value.find_first_not_of(" \t\r\n\f\v");
    return start == value.npos ? "" : value.substr(start, value.find_last_not_of(" \t\r\n\f\v") - start + 1);
}

class ControlLease {
public:
    explicit ControlLease(bool& busy) : busy_(&busy) { busy = true; }
    ControlLease(ControlLease&& other) noexcept : busy_(std::exchange(other.busy_, nullptr)) {}
    ~ControlLease() { if (busy_) *busy_ = false; }
private:
    bool* busy_;
};

Task<ControlLease> acquire_control(Connection& conn, bool& busy) {
    net::steady_timer timer(conn.executor());
    while (busy) {
        timer.expires_after(25ms);
        co_await timer.async_wait(net::use_awaitable);
    }
    co_return ControlLease(busy);
}

std::string csv_field(const Json& value) {
    if (value.is_null()) return {};
    auto text = value.is_string() ? value.get<std::string>() : value.dump();
    if (text.find_first_of(",\"\r\n") == text.npos) return text;
    std::string result = "\"";
    for (char c : text) {
        if (c == '"') result += '"';
        result += c;
    }
    return result + '"';
}
}

Json Admin::session(Connection& conn) {
    auto token = session_cookie(conn.header("Cookie"));
    if (token.empty()) throw HttpError(401, "请先登录。");
    auto session = store_.one("SELECT * FROM sessions WHERE token=? AND expires>?", {digest(token), now()});
    if (session.is_null()) throw HttpError(401, "登录已过期，请重新登录。");
    auto method = conn.request().method();
    if (method != http::verb::get && method != http::verb::head &&
        !secure_equal(conn.header("X-CSRF-Token"), session["csrf"].get_ref<const std::string&>()))
        throw HttpError(403, "会话验证失败，请刷新页面。");
    return session;
}

Task<> Admin::login(Connection& conn) {
    auto body = co_await read_json(conn);
    auto username = field_text(body, "username", 0, 64);
    auto password = field_text(body, "password", 0, 128);
    auto origin = conn.header("Origin");
    while (origin.ends_with('/')) origin.pop_back();
    if (!origin.empty() && origin != "http://" + conn.header("Host"))
        throw HttpError(403, "不允许的请求来源。");
    auto ip = conn.ip();
    auto attempts = store_.one("SELECT COUNT(*) n FROM login_attempts WHERE ip=? AND created>?", {ip, now() - 300});
    if (attempts["n"] >= 10) throw HttpError(429, "登录尝试过于频繁，请 5 分钟后重试。");
    auto user = store_.one("SELECT password FROM users WHERE username=?", {username});
    if (user.is_null() || !check_password(password, user["password"].get_ref<const std::string&>())) {
        store_.execute("INSERT INTO login_attempts VALUES (?,?)", {ip, now()});
        throw HttpError(401, "用户名或密码不正确。");
    }
    SessionResponse response;
    store_.transaction([&] {
        store_.execute("DELETE FROM login_attempts WHERE ip=?", {ip});
        store_.audit("account.login", ip);
        response = new_session(store_);
    });
    co_await conn.json(200, std::move(response.body), std::move(response.headers));
}

Task<> Admin::password(Connection& conn) {
    auto body = co_await read_json(conn);
    auto current = field_text(body, "current_password", 0, 128);
    auto replacement = field_text(body, "new_password", 10, 128);
    auto user = store_.one("SELECT username,password FROM users LIMIT 1");
    if (user.is_null() || !check_password(current, user["password"].get_ref<const std::string&>()))
        throw HttpError(400, "当前密码不正确。");
    auto hash = password_hash(replacement);
    SessionResponse response;
    store_.transaction([&] {
        store_.execute("UPDATE users SET password=? WHERE username=?", {hash, user["username"]});
        store_.execute("DELETE FROM sessions");
        store_.audit("account.password_changed");
        response = new_session(store_);
    });
    co_await conn.json(200, std::move(response.body), std::move(response.headers));
}

Task<> Admin::settings(Connection& conn) {
    auto body = co_await read_json(conn);
    const std::array fields{"served_model_name", "concurrency", "context_length", "thinking_enabled",
                            "autostart_inference", "apply_restart"};
    for (auto it = body.begin(); it != body.end(); ++it)
        if (std::find(fields.begin(), fields.end(), it.key()) == fields.end()) throw HttpError(422, "存在未知配置项。");
    auto alias = field_text(body, "served_model_name", 1, 128);
    if (alias.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-") != alias.npos)
        throw HttpError(422, "无效字段：served_model_name");
    auto concurrency = field_int(body, "concurrency", 1, 8);
    auto context = field_int(body, "context_length", 1024, 32768);
    auto thinking = field_bool(body, "thinking_enabled");
    auto autostart = field_bool(body, "autostart_inference");
    auto apply = !body.contains("apply_restart") || field_bool(body, "apply_restart");
    auto lease = co_await acquire_control(conn, control_busy_);
    auto limit = store_.capacity(concurrency).at("max_context").get<int>();
    if (context > limit)
        throw HttpError(400, std::to_string(concurrency) + " 并发下容量上限为 " + std::to_string(limit) + " token。");
    auto old = store_.config();
    auto applied = store_.applied(old);
    bool restart = applied.value("concurrency", old["concurrency"].get<int>()) != concurrency ||
        applied.value("context_length", old["context_length"].get<int>()) != context;
    Json next = {{"served_model_name", alias}, {"concurrency", concurrency}, {"context_length", context},
                 {"thinking_enabled", thinking}, {"autostart_inference", autostart}};
    bool changed_autostart = false, changed_config = false;
    std::optional<HttpError> failure;
    try {
        if (old["autostart_inference"] != autostart) {
            co_await runtime_.control(autostart ? "enable" : "disable");
            changed_autostart = true;
        }
        store_.set_meta("config", next);
        changed_config = true;
        auto state = runtime_.snapshot().at("state");
        if (restart && apply && (state == "running" || state == "loading")) {
            co_await runtime_.control("restart");
            runtime_.transition("loading");
        }
    } catch (const std::exception& error) {
        failure.emplace(500, error.what());
    }
    if (failure) {
        if (changed_config) store_.set_meta("config", old);
        if (changed_autostart) {
            try {
                co_await runtime_.control(autostart ? "disable" : "enable");
            } catch (const std::exception&) {}
        }
        throw *failure;
    }
    store_.audit("settings.updated", next.dump());
    Json result = {{"ok", true}, {"restart", restart}, {"config", std::move(next)}};
    co_await conn.json(200, std::move(result));
}

Task<> Admin::control(Connection& conn, std::string action) {
    if (action != "start" && action != "stop" && action != "restart") throw HttpError(400, "未知操作。");
    auto lease = co_await acquire_control(conn, control_busy_);
    try {
        co_await runtime_.control(action);
    } catch (const std::exception& error) {
        throw HttpError(500, error.what());
    }
    auto state = action == "stop" ? "stopping" : "loading";
    runtime_.transition(state);
    Json result = {{"ok", true}, {"state", state}};
    co_await conn.json(200, std::move(result));
}

Task<> Admin::keys(Connection& conn, std::string path) {
    auto method = conn.request().method();
    if (path == "/api/keys") {
        if (method == http::verb::get) {
            Json result = {{"keys", store_.keys()}};
            co_return co_await conn.json(200, std::move(result));
        }
        if (method != http::verb::post) throw HttpError(405, "不支持的请求方法。");
        auto body = co_await read_json(conn);
        auto name = field_text(body, "name", 1, 60);
        auto value = body.contains("value") ? field_text(body, "value", 0, 512) : "";
        if (value.empty()) value = "sk-mavi-" + random_hex(24);
        if (characters(value) < 12 || std::any_of(value.begin(), value.end(),
                [](unsigned char c) { return c <= 32 || c == 127; }))
            throw HttpError(400, "Key 至少 12 个字符且不能包含空格。");
        std::string id;
        try {
            id = store_.insert_key(strip(name), value);
        } catch (const std::exception&) {
            throw HttpError(400, "此 API Key 已存在。");
        }
        store_.audit("key.created", name);
        Json result = {{"id", id}, {"value", value}};
        co_return co_await conn.json(200, std::move(result));
    }
    auto id = path.substr(10);
    if (method == http::verb::get && id.ends_with("/reveal")) {
        id.resize(id.size() - 7);
        auto row = store_.one("SELECT encrypted FROM api_keys WHERE id=?", {id});
        if (row.is_null()) throw HttpError(404, "Key 不存在。");
        auto value = store_.decrypt(row["encrypted"].get_ref<const std::string&>());
        store_.audit("key.revealed", id);
        Json result = {{"value", value}};
        co_return co_await conn.json(200, std::move(result));
    }
    if (id.empty() || id.find('/') != id.npos) throw HttpError(404, "Key 不存在。");
    if (method == http::verb::patch) {
        auto body = co_await read_json(conn);
        auto enabled = field_bool(body, "enabled");
        store_.execute("UPDATE api_keys SET enabled=? WHERE id=?", {enabled, id});
        store_.audit(enabled ? "key.enabled" : "key.disabled", id);
    } else if (method == http::verb::delete_) {
        store_.execute("DELETE FROM api_keys WHERE id=?", {id});
        store_.audit("key.deleted", id);
    } else throw HttpError(405, "不支持的请求方法。");
    Json result = {{"ok", true}};
    co_await conn.json(200, std::move(result));
}

Task<> Admin::playground(Connection& conn) {
    auto row = store_.one("SELECT encrypted FROM api_keys WHERE enabled=1 ORDER BY created LIMIT 1");
    if (row.is_null()) throw HttpError(400, "请先启用一个 API Key。");
    auto body = co_await conn.read_body();
    Upstream upstream(conn.executor());
    conn.watch_disconnect(upstream.socket());
    try {
        co_await upstream.open(gateway_port_, http::verb::post, "/v1/chat/completions", std::move(body),
                               store_.decrypt(row["encrypted"].get_ref<const std::string&>()));
    } catch (const boost::system::system_error&) {
        if (conn.disconnected()) co_return;
        throw HttpError(503, "API 网关尚未就绪。");
    }
    if (conn.disconnected()) co_return;
    co_await conn.start_stream(upstream.status(), upstream.type());
    while (!upstream.done() && !conn.disconnected()) {
        auto chunk = co_await upstream.next();
        co_await conn.chunk(chunk);
    }
    if (!conn.disconnected()) co_await conn.end_stream();
}

Task<> Admin::export_csv(Connection& conn) {
    auto rows = store_.query("SELECT * FROM requests ORDER BY created DESC LIMIT 10000");
    std::string output = "\xef\xbb\xbfid,created,key_name,route,model,status,outcome,latency_ms,ttft_ms,input_tokens,output_tokens,stream\r\n";
    const std::array fields{"key_name", "route", "model", "status", "outcome", "latency_ms", "ttft_ms",
                            "input_tokens", "output_tokens", "stream"};
    for (const auto& row : rows) {
        auto time = static_cast<std::time_t>(row["created"].get<double>());
        std::tm utc{};
        gmtime_r(&time, &utc);
        char stamp[32]{};
        std::strftime(stamp, sizeof stamp, "%Y-%m-%dT%H:%M:%SZ", &utc);
        output += csv_field(row["id"]) + "," + stamp;
        for (auto field : fields) output += "," + csv_field(row[field]);
        output += "\r\n";
    }
    Headers headers = {{"Content-Disposition", "attachment; filename=mavi-requests.csv"}};
    co_await conn.send(200, std::move(output), "text/csv; charset=utf-8", std::move(headers));
}

Task<> Admin::handle(Connection& conn) {
    auto path = conn.path();
    auto method = conn.request().method();
    const bool get = method == http::verb::get;
    const bool post = method == http::verb::post;
    if (get || method == http::verb::head) {
        if (path == "/" || path == "/static/index.html") {
            Headers headers = {{"Cache-Control", "no-cache"}};
            co_return co_await conn.send(200, read_file(store_.root / "static/index.html"),
                                         "text/html; charset=utf-8", std::move(headers));
        }
        if (path == "/static/app.js")
            co_return co_await conn.send(200, read_file(store_.root / "static/app.js"), "text/javascript; charset=utf-8");
        if (path == "/static/styles.css")
            co_return co_await conn.send(200, read_file(store_.root / "static/styles.css"), "text/css; charset=utf-8");
    }
    if (get && path == "/api/health") {
        Json result = {{"status", "ok"}, {"product", "mavi_vllm管理平台"}};
        co_return co_await conn.json(200, std::move(result));
    }
    if (post && path == "/api/login") co_return co_await login(conn);
    if (!path.starts_with("/api/")) throw HttpError(404, "Not Found");
    auto current = session(conn);
    if (get && path == "/api/session") {
        Json result = store_.one("SELECT username FROM users LIMIT 1");
        result["csrf"] = current["csrf"];
        co_return co_await conn.json(200, std::move(result));
    }
    if (post && path == "/api/logout") {
        store_.execute("DELETE FROM sessions WHERE token=?", {current["token"]});
        Headers headers = {{"Set-Cookie", "mavi_session=; Max-Age=0; HttpOnly; Path=/; SameSite=Strict"}};
        Json result = {{"ok", true}};
        co_return co_await conn.json(200, std::move(result), std::move(headers));
    }
    if (post && path == "/api/password") co_return co_await password(conn);
    if (get && path == "/api/overview") {
        auto result = store_.usage();
        result["runtime"] = runtime_.snapshot();
        result["config"] = store_.config();
        result["applied"] = store_.meta("applied_config", Json::object());
        co_return co_await conn.json(200, std::move(result));
    }
    if (get && path == "/api/recommendations") co_return co_await conn.json(200, store_.recommendations());
    if (post && path == "/api/settings") co_return co_await settings(conn);
    if (post && path.starts_with("/api/control/")) co_return co_await control(conn, path.substr(13));
    if (path == "/api/keys" || path.starts_with("/api/keys/")) co_return co_await keys(conn, std::move(path));
    if (get && path == "/api/logs") {
        auto logs = co_await runtime_.logs();
        Json result = {{"text", std::move(logs)}, {"audit", store_.query("SELECT * FROM audit ORDER BY id DESC LIMIT 30")}};
        co_return co_await conn.json(200, std::move(result));
    }
    if (get && path == "/api/export") co_return co_await export_csv(conn);
    if (post && path == "/api/playground") co_return co_await playground(conn);
    throw HttpError(404, "Not Found");
}

void reset_admin(Store& store) {
    auto hash = password_hash("admin");
    store.transaction([&] {
        store.execute("DELETE FROM users");
        store.execute("INSERT INTO users VALUES (?,?)", {"admin", hash});
        store.execute("DELETE FROM sessions");
        store.audit("account.default_updated", "admin");
        store.write_bootstrap();
    });
}
}
