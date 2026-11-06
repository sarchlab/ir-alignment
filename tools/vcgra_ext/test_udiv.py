#!/usr/bin/env python3
"""Checks UDivComb against Python's unsigned // and % (the arithmetic DivRTL
performs), in pure-Python simulation and, with --verilator, after Verilog
translation + Verilator import."""

import argparse
import os
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pymtl3 import *  # noqa: E402,F401,F403
from pymtl3.passes.backends.verilog import VerilogTranslationImportPass  # noqa: E402
from pymtl3.stdlib.test_utils.test_helpers import finalize_verilator  # noqa: E402

from UDivComb import UDivComb  # noqa: E402


def vectors(nbits, n, seed=1):
  rng = random.Random(seed)
  top = (1 << nbits) - 1
  edge = [0, 1, 2, 3, top, top - 1, 1 << (nbits - 1), (1 << (nbits - 1)) - 1]
  out = [(a, b) for a in edge for b in edge if b != 0]
  while len(out) < n:
    a = rng.getrandbits(nbits)
    b = rng.getrandbits(rng.randint(1, nbits)) or 1
    out.append((a, b))
  return out


def check(nbits, n, verilator):
  dut = UDivComb(nbits)
  dut.elaborate()
  cwd = os.getcwd()
  tmp = tempfile.mkdtemp(prefix="udiv_")
  if verilator:
    os.chdir(tmp)
    dut.set_metadata(VerilogTranslationImportPass.enable, True)
    dut = VerilogTranslationImportPass()(dut)
  try:
    dut.apply(DefaultPassGroup(linetrace=False))
    dut.sim_reset()
    bad = 0
    for a, b in vectors(nbits, n):
      dut.dividend @= a
      dut.divisor @= b
      dut.sim_eval_combinational()
      q, r = int(dut.quotient), int(dut.remainder)
      if (q, r) != (a // b, a % b):
        bad += 1
        if bad <= 5:
          print(f"MISMATCH {a} / {b}: got q={q} r={r}, want q={a // b} r={a % b}")
    # Division by zero convention.
    dut.dividend @= 1234
    dut.divisor @= 0
    dut.sim_eval_combinational()
    if int(dut.quotient) != (1 << nbits) - 1 or int(dut.remainder) != 1234:
      bad += 1
      print("MISMATCH divide-by-zero convention")
  finally:
    if verilator:
      finalize_verilator(dut)
      os.chdir(cwd)
  print(f"UDivComb nbits={nbits} {'verilator' if verilator else 'sim'}: "
        f"{n} vectors, {bad} mismatches")
  return bad == 0


if __name__ == "__main__":
  ap = argparse.ArgumentParser()
  ap.add_argument("--verilator", action="store_true")
  ap.add_argument("-n", type=int, default=3000)
  args = ap.parse_args()
  ok = check(32, args.n, args.verilator)
  sys.exit(0 if ok else 1)
