#pragma once

#include "runtime.hpp"

namespace mavi {
class Admin {
public:
    Admin(Store& store, Runtime& runtime, unsigned short gateway_port)
        : store_(store), runtime_(runtime), gateway_port_(gateway_port) {}
    Task<> handle(Connection& conn);

private:
    Task<> login(Connection& conn);
    Task<> password(Connection& conn);
    Task<> settings(Connection& conn);
    Task<> control(Connection& conn, std::string action);
    Task<> keys(Connection& conn, std::string path);
    Task<> playground(Connection& conn);
    Task<> export_csv(Connection& conn);
    Json session(Connection& conn);
    Store& store_;
    Runtime& runtime_;
    unsigned short gateway_port_;
    bool control_busy_ = false;
};

void reset_admin(Store& store);
}
