#pragma once

#include "http.hpp"

namespace mavi {
class Gateway {
public:
    Gateway(Store& store, unsigned short upstream_port) : store_(store), upstream_port_(upstream_port) {}
    Task<> handle(Connection& conn);

private:
    Store& store_;
    unsigned short upstream_port_;
    int admitted_ = 0;
};
}
