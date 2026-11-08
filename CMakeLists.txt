cmake_minimum_required(VERSION 3.22)
project(mavi_vllm LANGUAGES CXX)

find_package(Boost 1.74 REQUIRED)
find_package(OpenSSL 3 REQUIRED COMPONENTS Crypto)
find_package(SQLite3 REQUIRED)
find_package(nlohmann_json 3.10 REQUIRED)
find_package(Threads REQUIRED)

add_executable(mavi-backend
    backend/main.cpp backend/store.cpp backend/crypto.cpp backend/http.cpp
    backend/admin.cpp backend/gateway.cpp backend/runtime.cpp)
target_compile_features(mavi-backend PRIVATE cxx_std_20)
target_compile_definitions(mavi-backend PRIVATE BOOST_ERROR_CODE_HEADER_ONLY)
target_compile_options(mavi-backend PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(mavi-backend PRIVATE Boost::boost OpenSSL::Crypto SQLite::SQLite3
    nlohmann_json::nlohmann_json Threads::Threads)
install(TARGETS mavi-backend RUNTIME DESTINATION bin)

include(CTest)
if(BUILD_TESTING)
    find_package(Python3 QUIET COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND)
        add_test(NAME platform
            COMMAND ${CMAKE_COMMAND} -E env MAVI_BACKEND_BINARY=$<TARGET_FILE:mavi-backend>
                ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_platform.py)
    endif()
endif()
