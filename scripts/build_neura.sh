#!/usr/bin/env bash
# Builds the neura submodule (mlir-neura-opt) against an LLVM build tree.
#
# Usage: LLVM_BUILD_DIR=/path/to/llvm-project/build scripts/build_neura.sh [build-dir]
# Env:   PYTHON (default: python3)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${LLVM_BUILD_DIR:?set LLVM_BUILD_DIR to the LLVM build directory}"
BUILD_DIR="${1:-${ROOT}/third_party/neura/build}"
PYTHON="${PYTHON:-$(command -v python3)}"
export LLVM_BUILD_DIR

LAUNCHER_FLAGS=()
if command -v ccache >/dev/null 2>&1; then
  LAUNCHER_FLAGS=(-DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache)
fi

cmake -G Ninja -S "${ROOT}/third_party/neura" -B "${BUILD_DIR}" \
  -DCMAKE_BUILD_TYPE=Release \
  -DPython3_EXECUTABLE="${PYTHON}" \
  -DPython_EXECUTABLE="${PYTHON}" \
  "${LAUNCHER_FLAGS[@]}"
# Only the optimizer driver is needed by the alignment pipeline.
cmake --build "${BUILD_DIR}" --target mlir-neura-opt
echo "built ${BUILD_DIR}/tools/mlir-neura-opt/mlir-neura-opt"
