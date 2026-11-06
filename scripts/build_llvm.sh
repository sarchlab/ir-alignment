#!/usr/bin/env bash
# Builds the LLVM/MLIR revision that the neura submodule is pinned to.
#
# Usage: scripts/build_llvm.sh <llvm-project-dir>
# Env:   PYTHON (default: python3), LLVM_JOBS (default: all cores)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LLVM_DIR="${1:?usage: build_llvm.sh <llvm-project-dir>}"
PYTHON="${PYTHON:-$(command -v python3)}"
LLVM_COMMIT="$("${ROOT}/scripts/llvm_commit.sh")"

echo "Building LLVM ${LLVM_COMMIT} into ${LLVM_DIR}"

if [[ ! -d "${LLVM_DIR}/.git" ]]; then
  git clone --depth 1 --filter=blob:none --revision="${LLVM_COMMIT}" \
    https://github.com/llvm/llvm-project.git "${LLVM_DIR}" 2>/dev/null || {
    # Older git without --revision support.
    mkdir -p "${LLVM_DIR}"
    git -C "${LLVM_DIR}" init -q
    git -C "${LLVM_DIR}" remote add origin https://github.com/llvm/llvm-project.git
    git -C "${LLVM_DIR}" fetch --depth 1 origin "${LLVM_COMMIT}"
    git -C "${LLVM_DIR}" checkout -q FETCH_HEAD
  }
fi

LAUNCHER_FLAGS=()
if command -v ccache >/dev/null 2>&1; then
  LAUNCHER_FLAGS=(-DCMAKE_C_COMPILER_LAUNCHER=ccache -DCMAKE_CXX_COMPILER_LAUNCHER=ccache)
fi

cmake -G Ninja -S "${LLVM_DIR}/llvm" -B "${LLVM_DIR}/build" \
  -DLLVM_ENABLE_PROJECTS="mlir;clang" \
  -DLLVM_BUILD_EXAMPLES=OFF \
  -DLLVM_TARGETS_TO_BUILD="Native" \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_ENABLE_ASSERTIONS=ON \
  -DCMAKE_C_COMPILER=clang \
  -DCMAKE_CXX_COMPILER=clang++ \
  -DCMAKE_CXX_STANDARD=17 \
  -DCMAKE_CXX_FLAGS="-std=c++17 -frtti" \
  -DLLVM_ENABLE_LLD=ON \
  -DMLIR_INSTALL_AGGREGATE_OBJECTS=ON \
  -DLLVM_ENABLE_RTTI=ON \
  -DMLIR_ENABLE_BINDINGS_PYTHON=ON \
  -DMLIR_BINDINGS_PYTHON_NB_DOMAIN=mlir \
  -DLLVM_INCLUDE_TESTS=ON \
  -DLLVM_INCLUDE_BENCHMARKS=OFF \
  -DPython3_EXECUTABLE="${PYTHON}" \
  -DPython_EXECUTABLE="${PYTHON}" \
  "${LAUNCHER_FLAGS[@]}"

cmake --build "${LLVM_DIR}/build" ${LLVM_JOBS:+-j "${LLVM_JOBS}"}
