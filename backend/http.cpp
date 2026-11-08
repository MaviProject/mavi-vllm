#include "http.hpp"

#include <boost/asio/co_spawn.hpp>
#include <boost/asio/detached.hpp>
#include <boost/asio/redirect_error.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/asio/use_awaitable.hpp>
#include <boost/asio/write.hpp>

#include <charconv>
#include <chrono>
#include <iostream>
#include <limits>

namespace mavi {
using namespace std::chrono_literals;
using net::use_awaitable;
using ErrorCode = boost::system::error_code;

Connection::Connection(tcp::socket socket, bool admin) : stream_(std::move(socket)), admin_(admin) {
    request_.header_limit(16384);
    request_.body_limit(std::numeric_limits<uint64_t>::max());
}

Task<> Connection::read_header() {
    stream_.expires_after(10s);
    co_await http::async_read_header(stream_, buffer_, request_, use_awaitable);
    stream_.expires_never();
}

Task<std::string> Connection::read_body(size_t limit) {
    auto limit_message = limit == body_limit ? "请求体超过 25 MB。" : "请求体超过允许上限。";
    if (auto size = request_.content_length(); size && *size > limit)
        throw HttpError(413, limit_message);
    request_.body_limit(limit);
    if (beast::iequals(request_.get()[http::field::expect], "100-continue")) {
        http::response<http::empty_body> proceed(http::status::continue_, request().version());
        stream_.expires_after(10s);
        co_await http::async_write(stream_, proceed, use_awaitable);
        stream_.expires_never();
    }
    // Cancel the read without closing the socket so a body deadline can return 408.
    auto deadline_hit = std::make_shared<bool>(false);
    net::steady_timer deadline(executor(), 30s);
    deadline.async_wait([self = weak_from_this(), deadline_hit](ErrorCode ec) {
        if (ec) return;
        if (auto connection = self.lock()) {
            *deadline_hit = true;
            ErrorCode ignored;
            connection->stream_.socket().cancel(ignored);
        }
    });
    ErrorCode ec;
    co_await http::async_read(stream_, buffer_, request_, net::redirect_error(use_awaitable, ec));
    deadline.cancel();
    if (*deadline_hit) throw HttpError(408, "请求体读取超时。");
    if (ec == http::error::body_limit) throw HttpError(413, limit_message);
    if (ec == http::error::partial_message || ec == net::error::connection_reset || ec == net::error::eof)
        throw HttpError(499, "客户端已断开。");
    if (ec) throw HttpError(400, "无效的请求体。");
    co_return std::move(request_.get().body());
}

std::string Connection::header(std::string_view name) const {
    return std::string(request()[beast::string_view(name.data(), name.size())]);
}

std::string Connection::path() const {
    return std::string(request().target().substr(0, request().target().find('?')));
}

std::string Connection::ip() const {
    ErrorCode ec;
    auto endpoint = stream_.socket().remote_endpoint(ec);
    return ec ? "" : endpoint.address().to_string();
}

void Connection::watch_disconnect(const std::shared_ptr<beast::tcp_stream>& upstream) {
    stream_.socket().async_wait(tcp::socket::wait_read,
        [self = weak_from_this(), peer = std::weak_ptr(upstream)](ErrorCode ec) {
            if (ec) return;
            auto connection = self.lock();
            auto source = peer.lock();
            if (!connection || !source) return;
            ErrorCode available_error;
            auto size = connection->stream_.socket().available(available_error);
            if (size && !available_error) return;
            connection->disconnected_ = true;
            source->close();
        });
}

void Connection::response_headers(http::fields& fields, std::string_view type, const Headers& extra) const {
    fields.set(http::field::content_type, beast::string_view(type.data(), type.size()));
    if (admin_) {
        fields.set("X-Content-Type-Options", "nosniff");
        fields.set("X-Frame-Options", "DENY");
        fields.set("Referrer-Policy", "same-origin");
        fields.set("Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
            "connect-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'");
        if (request().target().starts_with("/api/")) fields.set(http::field::cache_control, "no-store");
    }
    for (const auto& [name, value] : extra) fields.set(name, value);
}

Task<> Connection::send(int status, std::string body, std::string type, Headers extra) {
    http::response<http::string_body> response(static_cast<http::status>(status), request().version());
    response_headers(response.base(), type, extra);
    response.keep_alive(false);
    response.body() = std::move(body);
    response.prepare_payload();
    if (request().method() == http::verb::head) {
        response.body().clear();
        http::response_serializer<http::string_body> serializer(response);
        started_ = true;
        stream_.expires_after(10s);
        co_await http::async_write_header(stream_, serializer, use_awaitable);
    } else {
        started_ = true;
        stream_.expires_after(10s);
        co_await http::async_write(stream_, response, use_awaitable);
    }
}

Task<> Connection::json(int status, Json body, Headers extra) {
    co_await send(status, body.dump(-1, ' ', false, Json::error_handler_t::replace),
                  "application/json", std::move(extra));
}

Task<> Connection::start_stream(int status, std::string type, Headers extra) {
    http::response<http::empty_body> response(static_cast<http::status>(status), request().version());
    response_headers(response.base(), type, extra);
    response.keep_alive(false);
    response.chunked(request().version() >= 11);
    http::response_serializer<http::empty_body> serializer(response);
    started_ = true;
    stream_.expires_after(10s);
    co_await http::async_write_header(stream_, serializer, use_awaitable);
}

Task<> Connection::chunk(std::string_view body) {
    if (body.empty()) co_return;
    stream_.expires_after(10s);
    auto buffer = net::buffer(body.data(), body.size());
    if (request().version() >= 11) {
        auto chunk = http::make_chunk(buffer);
        co_await net::async_write(stream_, chunk, use_awaitable);
    } else co_await net::async_write(stream_, buffer, use_awaitable);
}

Task<> Connection::end_stream() {
    if (request().version() >= 11) {
        stream_.expires_after(10s);
        auto last = http::make_chunk_last();
        co_await net::async_write(stream_, last, use_awaitable);
    }
}

void Connection::finish() {
    ErrorCode ignored;
    stream_.socket().shutdown(tcp::socket::shutdown_send, ignored);
    stream_.close();
}

Upstream::Upstream(net::any_io_executor executor) : stream_(std::make_shared<beast::tcp_stream>(executor)) {
    parser_.body_limit(64 * 1024 * 1024);
    parser_.header_limit(16384);
}

Task<> Upstream::open(unsigned short port, http::verb method, std::string path, std::string body,
                      std::string key, int timeout) {
    timeout_ = timeout;
    stream_->expires_after(std::chrono::seconds(std::min(timeout, 5)));
    co_await stream_->async_connect(tcp::endpoint(net::ip::address_v4::loopback(), port), use_awaitable);
    {
        http::request<http::string_body> request(method, path, 11);
        request.set(http::field::host, "127.0.0.1:" + std::to_string(port));
        request.keep_alive(false);
        if (!key.empty()) request.set(http::field::authorization, "Bearer " + key);
        if (method == http::verb::post) {
            request.set(http::field::content_type, "application/json");
            request.body() = std::move(body);
            request.prepare_payload();
        }
        stream_->expires_after(std::chrono::seconds(timeout_));
        co_await http::async_write(*stream_, request, use_awaitable);
    }
    stream_->expires_after(std::chrono::seconds(timeout_));
    co_await http::async_read_header(*stream_, buffer_, parser_, use_awaitable);
}

Task<std::string_view> Upstream::next() {
    if (done()) co_return std::string_view{};
    parser_.get().body().data = bytes_.data();
    parser_.get().body().size = bytes_.size();
    stream_->expires_after(std::chrono::seconds(timeout_));
    ErrorCode ec;
    co_await http::async_read_some(*stream_, buffer_, parser_, net::redirect_error(use_awaitable, ec));
    if (ec && ec != http::error::need_buffer) throw boost::system::system_error(ec);
    co_return std::string_view(bytes_.data(), bytes_.size() - parser_.get().body().size);
}

Task<std::string> Upstream::read_all(size_t limit) {
    std::string result;
    while (!done()) {
        auto data = co_await next();
        if (result.size() + data.size() > limit) throw std::runtime_error("Upstream response exceeds buffer limit");
        result.append(data);
    }
    co_return result;
}

std::string Upstream::type() const {
    auto type = parser_.get()[http::field::content_type];
    return type.empty() ? "application/json" : std::string(type);
}

namespace {
Task<> serve(std::shared_ptr<Connection> conn, const Handler& handler) {
    std::optional<HttpError> failure;
    try {
        co_await conn->read_header();
        co_await handler(*conn);
    } catch (const HttpError& error) {
        if (error.status != 499) failure = error;
    } catch (const boost::system::system_error& error) {
        if (error.code().category() == make_error_code(http::error::bad_method).category() &&
            error.code() != http::error::end_of_stream && error.code() != http::error::partial_message)
            failure.emplace(400, "无效的 HTTP 请求。");
    } catch (const std::exception&) {
        std::cerr << "Backend request failed\n";
        failure.emplace(500, "服务器内部错误。");
    }
    if (failure && !conn->started() && !conn->disconnected()) {
        try {
            auto body = conn->path().starts_with("/v1/") ? api_error(failure->what(), failure->status) :
                Json{{"detail", failure->what()}};
            co_await conn->json(failure->status, std::move(body));
        } catch (const boost::system::system_error&) {}
    }
    conn->finish();
}

Task<> accept_connections(tcp::acceptor acceptor, bool admin, Handler handler) {
    auto handler_ref = std::make_shared<Handler>(std::move(handler));
    auto active = std::make_shared<size_t>(0);
    for (;;) {
        auto socket = co_await acceptor.async_accept(use_awaitable);
        if (*active >= 128) continue;
        auto conn = std::make_shared<Connection>(std::move(socket), admin);
        ++*active;
        net::co_spawn(acceptor.get_executor(), serve(conn, *handler_ref),
                      [active, handler_ref](std::exception_ptr) { --*active; });
    }
}
}

void listen(net::io_context& loop, std::string bind, unsigned short port, bool admin, Handler handler) {
    auto endpoint = tcp::endpoint(net::ip::make_address(bind), port);
    tcp::acceptor acceptor(loop);
    acceptor.open(endpoint.protocol());
    acceptor.set_option(net::socket_base::reuse_address(true));
    acceptor.bind(endpoint);
    acceptor.listen();
    net::co_spawn(loop, accept_connections(std::move(acceptor), admin, std::move(handler)),
                  [&loop](std::exception_ptr error) {
                      if (error) {
                          loop.stop();
                          std::rethrow_exception(error);
                      }
                  });
}

Json api_error(std::string_view message, int status, std::string_view kind) {
    return {{"error", {{"message", message}, {"type", kind}, {"code", status}}}};
}

unsigned short port_env(const char* name, unsigned short fallback) {
    auto raw = env(name, std::to_string(fallback));
    unsigned short port = 0;
    auto [end, error] = std::from_chars(raw.data(), raw.data() + raw.size(), port);
    if (error != std::errc() || end != raw.data() + raw.size() || !port)
        throw std::runtime_error(std::string("Invalid port: ") + name);
    return port;
}
}
