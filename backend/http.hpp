#pragma once

#include <utility>

#include <boost/asio/awaitable.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/beast/core/flat_buffer.hpp>
#include <boost/beast/core/tcp_stream.hpp>
#include <boost/beast/http.hpp>

#include <array>
#include <functional>
#include <memory>
#include <stdexcept>
#include <vector>

#include "store.hpp"

namespace mavi {
namespace net = boost::asio;
namespace beast = boost::beast;
namespace http = beast::http;
using net::ip::tcp;
template<class T = void> using Task = net::awaitable<T>;
using Headers = std::vector<std::pair<std::string, std::string>>;

struct HttpError : std::runtime_error {
    HttpError(int status, const std::string& message) : std::runtime_error(message), status(status) {}
    int status;
};

class Connection : public std::enable_shared_from_this<Connection> {
public:
    Connection(tcp::socket socket, bool admin);
    Task<> read_header();
    Task<std::string> read_body(size_t limit = body_limit);
    const auto& request() const { return request_.get(); }
    std::string header(std::string_view name) const;
    std::string path() const;
    std::string ip() const;
    bool started() const { return started_; }
    bool disconnected() const { return disconnected_; }
    net::any_io_executor executor() { return stream_.get_executor(); }
    void watch_disconnect(const std::shared_ptr<beast::tcp_stream>& upstream);
    Task<> send(int status, std::string body, std::string type, Headers headers = {});
    Task<> json(int status, Json body, Headers headers = {});
    Task<> start_stream(int status, std::string type, Headers headers = {});
    Task<> chunk(std::string_view body);
    Task<> end_stream();
    void finish();

private:
    void response_headers(http::fields& fields, std::string_view type, const Headers& extra) const;
    beast::tcp_stream stream_;
    beast::flat_buffer buffer_;
    http::request_parser<http::string_body> request_;
    bool admin_;
    bool started_ = false;
    bool disconnected_ = false;
};

class Upstream {
public:
    explicit Upstream(net::any_io_executor executor);
    Task<> open(unsigned short port, http::verb method, std::string path, std::string body = {},
                std::string key = {}, int timeout = 600);
    Task<std::string_view> next();
    Task<std::string> read_all(size_t limit = 16 * 1024 * 1024);
    int status() const { return parser_.get().result_int(); }
    std::string type() const;
    bool done() const { return parser_.is_done(); }
    const auto& socket() const { return stream_; }

private:
    std::shared_ptr<beast::tcp_stream> stream_;
    beast::flat_buffer buffer_;
    http::response_parser<http::buffer_body> parser_;
    std::array<char, 8192> bytes_{};
    int timeout_ = 600;
};

using Handler = std::function<Task<>(Connection&)>;
void listen(net::io_context& loop, std::string bind, unsigned short port, bool admin, Handler handler);
Json api_error(std::string_view message, int status, std::string_view kind = "invalid_request_error");
unsigned short port_env(const char* name, unsigned short fallback);
}
