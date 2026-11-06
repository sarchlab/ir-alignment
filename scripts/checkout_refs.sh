#!/usr/bin/env bash
# Checks out the submodules at the given commits (and their own submodules).
#
# Usage: scripts/checkout_refs.sh <neura_sha> <zeonica_sha> <vectorcgra_sha>
# Any argument may be empty to keep the pinned commit.
# Env:   SUBMODULES="neura zeonica vectorcgra" limits which ones are fetched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT}"

# Some nested submodules (e.g. zeonica's Zeonica_Testbench) use SSH URLs.
git config --global url."https://github.com/".insteadOf "git@github.com:"
git submodule sync --recursive
WANT="${SUBMODULES:-neura zeonica vectorcgra}"

checkout() {  # <path> <sha>
  local path="$1" sha="${2:-}"
  local key
  key="$(basename "${path}" | tr '[:upper:]' '[:lower:]')"
  [[ " ${WANT} " == *" ${key} "* ]] || return 0
  git submodule update --init "${path}"
  if [[ -n "${sha}" ]]; then
    git -C "${path}" fetch --quiet --depth=1 origin "${sha}" || git -C "${path}" fetch --quiet origin
    git -C "${path}" checkout --quiet --detach "${sha}"
  fi
  git -C "${path}" submodule sync --recursive --quiet
  git -C "${path}" submodule update --init --recursive --depth=1 --quiet || \
    git -C "${path}" submodule update --init --recursive --quiet
  echo "${path} @ $(git -C "${path}" rev-parse HEAD)"
}

checkout third_party/neura "${1:-}"
checkout third_party/zeonica "${2:-}"
checkout third_party/VectorCGRA "${3:-}"
