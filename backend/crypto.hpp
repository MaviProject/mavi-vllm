#pragma once

#include <string>
#include <string_view>

namespace mavi {
std::string random_hex(int size);
std::string session_token();
std::string digest(std::string_view value);
std::string password_hash(std::string_view password, std::string_view salt = {});
bool check_password(std::string_view password, std::string_view encoded);
bool secure_equal(std::string_view left, std::string_view right);
std::string fernet_key();
std::string seal(std::string_view key, std::string_view plaintext);
std::string unseal(std::string_view key, std::string_view token);
}
