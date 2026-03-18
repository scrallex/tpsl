#!/bin/sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-python3}"
CXX_BIN="${CXX:-g++}"
EXT_SUFFIX="$($PYTHON_BIN - <<'PY'
import sysconfig

print(sysconfig.get_config_var("EXT_SUFFIX") or ".so")
PY
)"
OUTPUT_PATH="${1:-manifold_engine${EXT_SUFFIX}}"

"$CXX_BIN" \
  -O3 \
  -Wall \
  -shared \
  -std=c++20 \
  -fPIC \
  $($PYTHON_BIN -m pybind11 --includes) \
  -Isrc/core \
  src/core/structural_entropy.cpp \
  src/core/byte_stream_manifold.cpp \
  src/core/bindings.cpp \
  -ltbb \
  -o "$OUTPUT_PATH"
