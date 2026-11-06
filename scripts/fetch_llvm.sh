#!/usr/bin/env bash
# Installs the prebuilt LLVM/MLIR package for neura's pinned LLVM commit.
#
# Usage: scripts/fetch_llvm.sh <dest-dir> [tarball]
#   dest-dir  becomes <dest-dir>/build (use it as LLVM_BUILD_DIR)
#   tarball   a local llvm-<commit>.tar.zst; when omitted it is downloaded
#             from this repo's GitHub release "llvm-<commit>" (needs gh).
# Exits 2 when no package exists for the commit, so callers can fall back
# to scripts/build_llvm.sh.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:?usage: fetch_llvm.sh <dest-dir> [tarball]}"
TARBALL="${2:-}"
COMMIT="$("${ROOT}/scripts/llvm_commit.sh")"
TAG="llvm-${COMMIT}"
ASSET="llvm-${COMMIT}-$(uname -m)-ubuntu22.04.tar.zst"

if [[ -z "${TARBALL}" ]]; then
  TMP="$(mktemp -d)"
  if ! gh release download "${TAG}" --pattern "${ASSET}" --dir "${TMP}" ${GH_REPO:+--repo "${GH_REPO}"}; then
    echo "no prebuilt LLVM package ${TAG}/${ASSET}" >&2
    exit 2
  fi
  TARBALL="${TMP}/${ASSET}"
fi

mkdir -p "${DEST}"
DEST="$(cd "${DEST}" && pwd)"
zstd -dc "${TARBALL}" | tar -C "${DEST}" --strip-components=1 -xf -

# Relocate: the build tree's CMake files hard-code the absolute path it was
# built at.
OLD="$(cat "${DEST}/.ir-alignment-prefix")"
if [[ "${OLD}" != "${DEST}" ]]; then
  grep -rlI --null -F "${OLD}" "${DEST}/build" --include='*.cmake' --include='*.txt' \
    --include='*.json' --include='*.cfg' --include='*.py' --include='*.inc' 2>/dev/null \
    | xargs -0 -r sed -i "s#${OLD}#${DEST}#g"
  echo "${DEST}" > "${DEST}/.ir-alignment-prefix"
fi
echo "LLVM ${COMMIT} ready at ${DEST}/build"
