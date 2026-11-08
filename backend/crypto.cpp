#include "crypto.hpp"

#include <openssl/crypto.h>
#include <openssl/evp.h>
#include <openssl/hmac.h>
#include <openssl/rand.h>
#include <openssl/sha.h>
#include <malloc.h>

#include <algorithm>
#include <array>
#include <ctime>
#include <cstdint>
#include <memory>
#include <stdexcept>

namespace mavi {
namespace {
using Bytes = std::basic_string<unsigned char>;

const unsigned char* bytes(std::string_view value) {
    return reinterpret_cast<const unsigned char*>(value.data());
}

Bytes random_bytes(int size) {
    Bytes result(size, 0);
    if (RAND_bytes(result.data(), size) != 1) throw std::runtime_error("RAND_bytes failed");
    return result;
}

std::string hex(const unsigned char* data, size_t size) {
    constexpr std::string_view alphabet = "0123456789abcdef";
    std::string result;
    result.reserve(size * 2);
    for (size_t i = 0; i < size; ++i) {
        result += alphabet[data[i] >> 4];
        result += alphabet[data[i] & 15];
    }
    return result;
}

std::string base64(const Bytes& data) {
    std::string result(4 * ((data.size() + 2) / 3) + 1, '\0');
    auto size = EVP_EncodeBlock(reinterpret_cast<unsigned char*>(result.data()), data.data(), data.size());
    result.resize(size);
    std::replace(result.begin(), result.end(), '+', '-');
    std::replace(result.begin(), result.end(), '/', '_');
    return result;
}

Bytes decode64(std::string_view value) {
    if (value.empty() || value.size() % 4) throw std::runtime_error("Invalid Fernet encoding");
    std::string input(value);
    std::replace(input.begin(), input.end(), '-', '+');
    std::replace(input.begin(), input.end(), '_', '/');
    Bytes output(input.size() / 4 * 3, 0);
    int size = EVP_DecodeBlock(output.data(), bytes(input), input.size());
    if (size < 0) throw std::runtime_error("Invalid Fernet encoding");
    if (input.ends_with("==")) size -= 2;
    else if (input.ends_with('=')) --size;
    output.resize(size);
    return output;
}

Bytes decode_key(std::string_view value) {
    auto key = decode64(value);
    if (key.size() != 32) throw std::runtime_error("Invalid Fernet key length");
    return key;
}

Bytes sign(const Bytes& key, const Bytes& data, size_t length) {
    Bytes signature(SHA256_DIGEST_LENGTH, 0);
    if (!HMAC(EVP_sha256(), key.data(), 16, data.data(), length, signature.data(), nullptr))
        throw std::runtime_error("HMAC failed");
    return signature;
}

Bytes crypt(bool encrypt, const unsigned char* key, const unsigned char* iv,
            const unsigned char* input, size_t size) {
    std::unique_ptr<EVP_CIPHER_CTX, decltype(&EVP_CIPHER_CTX_free)>
        ctx(EVP_CIPHER_CTX_new(), EVP_CIPHER_CTX_free);
    Bytes output(size + 16, 0);
    int length = 0, tail = 0;
    if (!ctx || EVP_CipherInit_ex(ctx.get(), EVP_aes_128_cbc(), nullptr, key, iv, encrypt) != 1 ||
        EVP_CipherUpdate(ctx.get(), output.data(), &length, input, size) != 1 ||
        EVP_CipherFinal_ex(ctx.get(), output.data() + length, &tail) != 1)
        throw std::runtime_error("Fernet cipher operation failed");
    output.resize(length + tail);
    return output;
}
}

std::string random_hex(int size) {
    auto data = random_bytes(size);
    return hex(data.data(), data.size());
}

std::string session_token() {
    auto token = base64(random_bytes(32));
    token.erase(token.find('='));
    return token;
}

std::string digest(std::string_view value) {
    std::array<unsigned char, SHA256_DIGEST_LENGTH> hash{};
    SHA256(bytes(value), value.size(), hash.data());
    return hex(hash.data(), hash.size());
}

bool secure_equal(std::string_view left, std::string_view right) {
    return left.size() == right.size() && CRYPTO_memcmp(left.data(), right.data(), left.size()) == 0;
}

std::string password_hash(std::string_view password, std::string_view salt) {
    constexpr std::string_view alphabet = "0123456789abcdef";
    auto salt_hex = salt.empty() ? random_hex(16) : std::string(salt);
    std::array<unsigned char, 16> raw{};
    if (salt_hex.size() != raw.size() * 2) throw std::runtime_error("Invalid password salt");
    for (size_t i = 0; i < raw.size(); ++i) {
        auto hi = alphabet.find(salt_hex[i * 2]), lo = alphabet.find(salt_hex[i * 2 + 1]);
        if (hi == alphabet.npos || lo == alphabet.npos) throw std::runtime_error("Invalid password salt");
        raw[i] = (hi << 4) | lo;
    }
    std::array<unsigned char, 64> hash{};
    int result = EVP_PBE_scrypt(password.data(), password.size(), raw.data(), raw.size(),
                                16384, 8, 1, 32 * 1024 * 1024, hash.data(), hash.size());
    // Return the freed scrypt scratch pages between infrequent login operations.
    malloc_trim(0);
    if (result != 1)
        throw std::runtime_error("scrypt failed");
    return salt_hex + ':' + hex(hash.data(), hash.size());
}

bool check_password(std::string_view password, std::string_view encoded) {
    return encoded.size() == 161 && secure_equal(password_hash(password, encoded.substr(0, 32)), encoded);
}

std::string fernet_key() {
    return base64(random_bytes(32));
}

// Keep the existing Fernet wire format so both releases can read persisted keys.
std::string seal(std::string_view key_text, std::string_view plaintext) {
    auto key = decode_key(key_text);
    auto iv = random_bytes(16);
    auto timestamp = static_cast<uint64_t>(std::time(nullptr));
    Bytes token(1, 0x80);
    for (int shift = 56; shift >= 0; shift -= 8) token += static_cast<unsigned char>(timestamp >> shift);
    token += iv;
    token += crypt(true, key.data() + 16, iv.data(), bytes(plaintext), plaintext.size());
    token += sign(key, token, token.size());
    return base64(token);
}

std::string unseal(std::string_view key_text, std::string_view encoded) {
    auto key = decode_key(key_text);
    auto token = decode64(encoded);
    constexpr size_t header = 25, signature = SHA256_DIGEST_LENGTH;
    if (token.size() < header + 16 + signature || token[0] != 0x80 ||
        (token.size() - header - signature) % 16)
        throw std::runtime_error("Invalid Fernet token");
    auto signed_size = token.size() - signature;
    auto expected = sign(key, token, signed_size);
    if (CRYPTO_memcmp(expected.data(), token.data() + signed_size, signature))
        throw std::runtime_error("Invalid Fernet signature");
    auto plaintext = crypt(false, key.data() + 16, token.data() + 9, token.data() + header, signed_size - header);
    return {reinterpret_cast<const char*>(plaintext.data()), plaintext.size()};
}
}
