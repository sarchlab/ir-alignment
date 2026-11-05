#!/usr/bin/env bash
# Installs what VectorCGRA's simulation/Verilator flow needs on an Ubuntu
# runner, into the active Python. Mirrors VectorCGRA's own CI dependencies.
set -euo pipefail
sudo apt-get update -qq
sudo apt-get install -y -qq verilator graphviz libffi-dev pkg-config
python -m pip install --upgrade pip wheel
python -m pip install py==1.11.0 hypothesis pytest py-markdown-table PyYAML
python -m pip install -U "git+https://github.com/tancheng/pymtl3.1@yo-struct-list-fix"
ver="$(verilator --version | awk '{print $2}')"
echo "verilator ${ver}"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "VERILATOR_VERSION=${ver}" >> "$GITHUB_ENV"
  echo "PYMTL_VERILATOR_INCLUDE_DIR=/usr/share/verilator/include" >> "$GITHUB_ENV"
fi
