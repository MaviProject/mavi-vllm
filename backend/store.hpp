#pragma once

#include <filesystem>
#include <functional>
#include <initializer_list>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

struct sqlite3;

namespace mavi {
using Json = nlohmann::json;
inline constexpr std::string_view model = "liuxing";
inline constexpr std::string_view engine_unit = "qwen3.8-vllm.service";
inline constexpr auto body_limit = 25 * 1024 * 1024;

double now();
std::string env(const char* name, std::string fallback);
std::string read_file(const std::filesystem::path& path);
Json parse_json(std::string_view input);
int image_max_pixels(int context);

class Store {
public:
    explicit Store(std::filesystem::path root);
    ~Store();
    Store(const Store&) = delete;
    Store& operator=(const Store&) = delete;

    const std::filesystem::path root;
    const std::filesystem::path data;
    using Params = std::initializer_list<Json>;
    void execute(const char* sql, Params params = {});
    Json query(const char* sql, Params params = {});
    Json one(const char* sql, Params params = {});
    void transaction(const std::function<void()>& write);
    Json meta(std::string_view key, Json fallback = nullptr);
    void set_meta(std::string_view key, const Json& value);
    Json config();
    Json applied(const Json& config);
    Json lookup_key(std::string_view value);
    std::string insert_key(std::string_view name, std::string_view value);
    std::string decrypt(std::string_view value);
    const std::string& internal_key() const { return internal_; }
    void audit(std::string_view action, std::string_view detail = {});
    void record(const Json& request);
    Json keys();
    Json usage();
    void write_bootstrap();
    Json recommendations();
    Json capacity(int concurrency);
    void calibrate(const Json& calibration);

private:
    sqlite3* db_ = nullptr;
    std::string key_;
    std::string internal_;
    void script(const char* sql);
    void initialize();
};
}
