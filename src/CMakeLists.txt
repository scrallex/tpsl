cmake_minimum_required(VERSION 3.18)
project(sep_trader LANGUAGES CXX C)

add_definitions(-DGLM_COMPILER=0)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Fetch spdlog
include(FetchContent)

set(SPDLOG_BUILD_SHARED FALSE)

FetchContent_Declare(
    spdlog
    GIT_REPOSITORY https://github.com/gabime/spdlog.git
    GIT_TAG v1.12.0
    FIND_PACKAGE_ARGS
)

FetchContent_MakeAvailable(spdlog)

# Gather C++ source files only from the tracked native core
file(GLOB_RECURSE CXX_SOURCES
    "core/*.cpp"
)

# No C source files are currently needed - all utilities use C++/spdlog
set(C_SOURCES)

# Exclude facade_original.cpp only (OANDA files are needed for frontend)
list(FILTER CXX_SOURCES EXCLUDE REGEX ".*/facade_original\\.cpp$")
# list(FILTER CXX_SOURCES EXCLUDE REGEX ".*/core/structural_entropy\\.cpp$")

# Separate main files from library source files
set(LIB_CXX_SOURCES)
foreach(file ${CXX_SOURCES})
    if(NOT file MATCHES "main.cpp$")
        list(APPEND LIB_CXX_SOURCES ${file})
    endif()
endforeach()

# Create C++ library with PCH (no C sources needed)
add_sep_library(sep_lib SOURCES ${LIB_CXX_SOURCES})
# Link core deps privately to avoid leaking shared deps to dependents
target_link_libraries(sep_lib
    PRIVATE
        sep_core_deps
        sep_fetchcontent_deps
)
# Note: Include directories are handled by add_sep_library template with proper generator expressions
# Note: sep_fetchcontent_deps is PRIVATE - available for building sep_lib but not part of export interface

# Collect public headers for installation from the tracked native core
file(GLOB_RECURSE SEP_LIB_PUBLIC_HEADERS
    "core/*.h" "core/*.hpp" "core/*.cuh"
)
set_target_properties(sep_lib PROPERTIES PUBLIC_HEADER "${SEP_LIB_PUBLIC_HEADERS}")

install(TARGETS sep_lib
    PUBLIC_HEADER DESTINATION include/sep
    ARCHIVE DESTINATION lib
    LIBRARY DESTINATION lib
    RUNTIME DESTINATION bin
)

# (Install rules moved to end of file to ensure targets exist)

# Legacy standalone C++ apps were removed from this checkout.
