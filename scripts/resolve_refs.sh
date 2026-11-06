#!/usr/bin/env bash
# Resolves which commit of each tool to test and writes KEY=VALUE lines
# (suitable for $GITHUB_OUTPUT / $GITHUB_ENV) to stdout.
#
#   MODE=pinned  -> the commits recorded in this repo's submodules
#   MODE=latest  -> the current head of each submodule's tracked branch
#
# Explicit overrides (a branch, tag, or SHA) win over both:
#   NEURA_REF, ZEONICA_REF, VECTORCGRA_REF
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${MODE:-pinned}"

resolve() {  # <name> <path> <override>
  local name="$1" path="$2" override="${3:-}"
  local url branch sha
  url="$(git -C "${ROOT}" config -f .gitmodules "submodule.${path}.url")"
  branch="$(git -C "${ROOT}" config -f .gitmodules "submodule.${path}.branch" || echo main)"
  if [[ -n "${override}" ]]; then
    if [[ "${override}" =~ ^[0-9a-f]{40}$ ]]; then
      sha="${override}"
    else
      sha="$(git ls-remote "${url}" "refs/heads/${override}" "refs/tags/${override}" | head -1 | cut -f1)"
      [[ -n "${sha}" ]] || { echo "cannot resolve ${name} ref '${override}'" >&2; exit 1; }
    fi
  elif [[ "${MODE}" == "latest" ]]; then
    sha="$(git ls-remote "${url}" "refs/heads/${branch}" | cut -f1)"
    [[ -n "${sha}" ]] || { echo "cannot resolve ${name} branch ${branch}" >&2; exit 1; }
  else
    sha="$(git -C "${ROOT}" ls-files -s "${path}" | awk '{print $2}')"
  fi
  echo "${name}_sha=${sha}"
}

resolve neura third_party/neura "${NEURA_REF:-}"
resolve zeonica third_party/zeonica "${ZEONICA_REF:-}"
resolve vectorcgra third_party/VectorCGRA "${VECTORCGRA_REF:-}"
