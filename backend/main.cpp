#include "admin.hpp"
#include "gateway.hpp"

#include <boost/asio/co_spawn.hpp>
#include <boost/asio/io_context.hpp>
#include <boost/asio/signal_set.hpp>

#include <csignal>
#include <iostream>
#include <sys/stat.h>

int main(int argc, char** argv) {
    using namespace mavi;
    const std::string_view mode = argc == 2 ? argv[1] : "";
    if (mode != "admin" && mode != "gateway" && mode != "engine" && mode != "reset-admin") {
        std::cerr << "Usage: mavi-backend {admin|gateway|engine|reset-admin}\n";
        return 2;
    }
    umask(0077);
    try {
        Store store(env("MAVI_PLATFORM_ROOT", std::filesystem::current_path().string()));
        if (mode == "engine") launch_engine(store);
        if (mode == "reset-admin") {
            reset_admin(store);
            std::cout << "Default web account updated; sessions invalidated.\n";
            return 0;
        }
        std::signal(SIGPIPE, SIG_IGN);
        auto upstream = port_env("MAVI_UPSTREAM_PORT", 8000);
        auto gateway_port = port_env("MAVI_GATEWAY_PORT", 80);
        Runtime runtime(store, upstream);
        Admin admin(store, runtime, gateway_port);
        Gateway gateway(store, upstream);
        net::io_context loop(1);
        net::signal_set signals(loop, SIGINT, SIGTERM);
        signals.async_wait([&loop](boost::system::error_code error, int) { if (!error) loop.stop(); });
        auto bind = env("MAVI_BIND_ADDRESS", "0.0.0.0");
        if (mode == "admin") {
            listen(loop, bind, port_env("MAVI_ADMIN_PORT", 20204), true,
                   [&admin](Connection& conn) { return admin.handle(conn); });
            net::co_spawn(loop, runtime.monitor(), [&loop](std::exception_ptr error) {
                if (error) {
                    loop.stop();
                    std::rethrow_exception(error);
                }
            });
        } else listen(loop, bind, gateway_port, false, [&gateway](Connection& conn) { return gateway.handle(conn); });
        loop.run();
    } catch (const std::exception& error) {
        std::cerr << "mavi-backend: " << error.what() << '\n';
        return 1;
    }
}
