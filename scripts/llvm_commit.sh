#!/usr/bin/env bash
# Prints the LLVM commit that the neura submodule pins in its own CI workflow,
# so ir-alignment never drifts from neura's toolchain.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WF="${ROOT}/third_party/neura/.github/workflows/main.yml"
commit="$(grep -m1 -E '^\s*LLVM_COMMIT:' "${WF}" | sed -E 's/.*"([0-9a-f]{40})".*/\1/')"
if [[ ! "${commit}" =~ ^[0-9a-f]{40}$ ]]; then
  # Fall back to the README pin.
  commit="$(grep -m1 -oE 'pinned to LLVM commit `[0-9a-f]{40}`' "${ROOT}/third_party/neura/README.md" | grep -oE '[0-9a-f]{40}')"
fi
[[ "${commit}" =~ ^[0-9a-f]{40}$ ]] || { echo "cannot determine LLVM commit" >&2; exit 1; }
echo "${commit}"
