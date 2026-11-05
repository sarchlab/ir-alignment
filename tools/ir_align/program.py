"""Turns Neura's generated YAML into a concrete, simulator-ready program.

Neura leaves function arguments symbolic (``arg0``, ``arg1``, ...). Both
simulators need concrete values, so pointer arguments are replaced with the
array base address from the shared memory layout and scalar arguments with
their value. The result is written once and consumed by *both* simulators.
"""

import re
from pathlib import Path
from typing import Dict, List, Tuple

import yaml

from .spec import KernelSpec

_ARG_RE = re.compile(r"^%?(arg\d+)$")
_BAD_OPERANDS = ("UNRESOLVED", "UNSUPPORTED_CONSTANT")


def _iter_operations(root):
  for core in root["array_config"]["cores"]:
    for entry in core.get("entries", []):
      for instruction in entry.get("instructions", []):
        for op in instruction.get("operations", []):
          yield core, instruction, op


def patch_program(spec: KernelSpec, neura_yaml: Path, out_yaml: Path) -> Dict:
  root = yaml.safe_load(neura_yaml.read_text())
  if not root or "array_config" not in root:
    raise ValueError(f"{neura_yaml} has no array_config")
  values = spec.arg_operand_values()
  replaced: List[Tuple[str, int]] = []
  problems: List[str] = []
  opcodes = set()

  for core, _, op in _iter_operations(root):
    opcodes.add(op["opcode"])
    for key in ("src_operands", "dst_operands"):
      for operand in op.get(key, []) or []:
        text = str(operand.get("operand", ""))
        m = _ARG_RE.match(text)
        if m:
          name = m.group(1)
          if name not in values:
            problems.append(f"{op['opcode']} on PE({core['column']},{core['row']}) uses {name}, "
                            f"but the kernel spec declares only {len(spec.args)} args")
            continue
          operand["operand"] = f"#{values[name]}"
          replaced.append((name, values[name]))
        elif any(bad in text for bad in _BAD_OPERANDS):
          problems.append(f"{op['opcode']} on PE({core['column']},{core['row']}) has operand {text}")

  if problems:
    raise ValueError("Neura output cannot be made concrete:\n  " + "\n  ".join(problems))

  out_yaml.write_text(yaml.safe_dump(root, sort_keys=False))
  cfg = root["array_config"]
  return {
      "compiled_ii": int(cfg["compiled_ii"]),
      "columns": int(cfg["columns"]),
      "rows": int(cfg["rows"]),
      "opcodes": sorted(opcodes),
      "arg_replacements": sorted(set(replaced)),
      "num_cores": len(cfg["cores"]),
  }
