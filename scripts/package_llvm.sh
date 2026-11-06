#!/usr/bin/env bash
# Packs an LLVM/MLIR build (from scripts/build_llvm.sh) into a slim,
# relocatable tarball containing only what ir-alignment needs:
#   * everything required to build neura out of tree (headers, static
#     libraries, CMake packages, TableGen tools, MLIR Python binding sources),
#   * the tools the pipeline runs (clang, llvm-extract, mlir-translate,
#     FileCheck, llvm-lit).
# Object files and the ~140 other LLVM/MLIR executables are dropped.
#
# Usage: scripts/package_llvm.sh <llvm-project-dir> <out.tar.zst>
# The tarball records its original location in .ir-alignment-prefix so
# scripts/fetch_llvm.sh can relocate it.
set -euo pipefail
SRC="$(cd "${1:?usage: package_llvm.sh <llvm-project-dir> <out.tar.zst>}" && pwd)"
OUT="${2:?usage: package_llvm.sh <llvm-project-dir> <out.tar.zst>}"
OUT="$(cd "$(dirname "${OUT}")" && pwd)/$(basename "${OUT}")"
B="${SRC}/build"

KEEP_BIN=(clang clang++ clang-[0-9]* llvm-extract mlir-translate FileCheck
          llvm-lit not count llvm-config
          llvm-tblgen mlir-tblgen mlir-pdll mlir-linalg-ods-yaml-gen
          mlir-src-sharder mlir-irdl-to-cpp clang-tblgen)

STAGE="$(mktemp -d)"
trap 'rm -rf "${STAGE}"' EXIT
mkdir -p "${STAGE}/llvm-project/build/bin"

cd "${SRC}"
git rev-parse HEAD > "${STAGE}/llvm-project/.llvm-commit" 2>/dev/null || true
echo "${SRC}" > "${STAGE}/llvm-project/.ir-alignment-prefix"

# Source-tree pieces referenced by the build tree's CMake packages.
tar -C "${SRC}" -cf - \
  llvm/include llvm/cmake llvm/utils/lit \
  mlir/include mlir/cmake mlir/python mlir/lib/Bindings \
  mlir/tools/mlir-linalg-ods-gen \
  clang/include cmake \
  2>/dev/null | tar -C "${STAGE}/llvm-project" -xf -

# Build tree minus object files and unneeded executables.
tar -C "${B}" -cf - --exclude='*.o' --exclude='./bin' --exclude='./tools/clang/stage2-*' \
  --exclude='./unittests' --exclude='./benchmarks' --exclude='./test' --exclude='./CMakeFiles' \
  --exclude='./tools/*/test' --exclude='./tools/mlir/test' \
  . | tar -C "${STAGE}/llvm-project/build" -xf -
shopt -s nullglob
for pattern in "${KEEP_BIN[@]}"; do
  for f in "${B}"/bin/${pattern}; do
    [[ -e "${f}" ]] && cp -a "${f}" "${STAGE}/llvm-project/build/bin/"
  done
done

du -sh "${STAGE}/llvm-project"
tar -C "${STAGE}" -cf - llvm-project | zstd -T0 -10 -q -o "${OUT}" -f
ls -lh "${OUT}"
