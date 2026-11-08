#pragma once

#include "http.hpp"

namespace mavi {
class Runtime {
public:
    Runtime(Store& store, unsigned short upstream_port) : store_(store), upstream_port_(upstream_port) {}
    Task<> monitor();
    Task<> control(std::string action);
    Task<std::string> logs();
    void transition(std::string_view state);
    const Json& snapshot() const { return cache_; }

private:
    Store& store_;
    unsigned short upstream_port_;
    Json cache_ = {{"state", "loading"}, {"hardware", Json::object()}, {"metrics", Json::object()}, {"updated", 0}};
    unsigned transition_id_ = 0;
};

[[noreturn]] void launch_engine(Store& store);
}
