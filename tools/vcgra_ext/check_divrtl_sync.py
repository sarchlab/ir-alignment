#!/usr/bin/env python3
"""Fails if TranslatableDivRTL's control logic has drifted from upstream DivRTL.

Both `comb_logic` update blocks are parsed; in upstream's, every
`<x> // <y>` is replaced by `s.udiv.quotient` (the only intended difference,
since UDivComb computes exactly that quotient). The resulting ASTs must match.
"""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM = ROOT / "third_party" / "VectorCGRA" / "fu" / "single" / "DivRTL.py"
OURS = Path(__file__).resolve().parent / "TranslatableDivRTL.py"


class _ReplaceFloorDiv(ast.NodeTransformer):

  def visit_BinOp(self, node):
    self.generic_visit(node)
    if isinstance(node.op, ast.FloorDiv):
      return ast.parse("s.udiv.quotient", mode="eval").body
    return node


def comb_logic(path, replace_floordiv):
  tree = ast.parse(path.read_text())
  for node in ast.walk(tree):
    if isinstance(node, ast.FunctionDef) and node.name == "comb_logic":
      if replace_floordiv:
        node = _ReplaceFloorDiv().visit(node)
      return ast.dump(node, annotate_fields=False, include_attributes=False)
  raise SystemExit(f"no comb_logic update block in {path}")


def main():
  upstream = comb_logic(UPSTREAM, replace_floordiv=True)
  ours = comb_logic(OURS, replace_floordiv=False)
  if upstream != ours:
    print("TranslatableDivRTL is out of sync with VectorCGRA's DivRTL comb_logic.\n"
          f"Re-copy the control logic from {UPSTREAM.relative_to(ROOT)} into "
          f"{OURS.relative_to(ROOT)} (keeping `s.udiv.quotient` in place of `//`).")
    return 1
  print("TranslatableDivRTL matches upstream DivRTL (modulo the divider datapath).")
  return 0


if __name__ == "__main__":
  sys.exit(main())
