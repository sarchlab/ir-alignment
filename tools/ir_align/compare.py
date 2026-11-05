"""Compares a simulator result against the native golden reference."""

from typing import Dict, List

from .spec import KernelSpec

MAX_REPORTED_MISMATCHES = 12


def compare(spec: KernelSpec, golden: Dict, result: Dict) -> Dict:
  arrays: List[Dict] = []
  total_mismatches = 0
  memory = result.get("memory") or []
  for arr in spec.arrays:
    mismatches = []
    for k in range(arr.size):
      addr = arr.base + k
      expected = golden["memory"][addr]
      got = memory[addr] if addr < len(memory) else None
      if got != expected:
        mismatches.append({"index": k, "addr": addr, "expected": expected, "got": got})
    total_mismatches += len(mismatches)
    arrays.append({
        "name": arr.name,
        "size": arr.size,
        "base": arr.base,
        "changed_by_kernel": golden["memory"][arr.base:arr.base + arr.size] != arr.init,
        "mismatches": len(mismatches),
        "examples": mismatches[:MAX_REPORTED_MISMATCHES],
    })

  # Guard words between arrays must stay untouched (catches stray stores).
  in_array = set()
  for arr in spec.arrays:
    in_array.update(range(arr.base, arr.base + arr.size))
  stray = [a for a in range(min(len(memory), spec.memory_words))
           if a not in in_array and memory[a] != golden["memory"][a]]

  ret = None
  if spec.returns_value:
    ret = {"expected": golden["return"], "got": result.get("return"),
           "ok": golden["return"] == result.get("return")}
    if result.get("returned") is False:
      ret["ok"] = False
      ret["note"] = "kernel never executed RETURN"

  passed = total_mismatches == 0 and not stray and (ret is None or ret["ok"])
  if result.get("timed_out"):
    passed = False
  return {
      "passed": passed,
      "mismatched_words": total_mismatches,
      "stray_writes": stray[:MAX_REPORTED_MISMATCHES],
      "arrays": arrays,
      "return": ret,
      "cycles": result.get("cycles"),
      "timed_out": bool(result.get("timed_out")),
  }


def agreement(spec: KernelSpec, a: Dict, b: Dict) -> Dict:
  """Checks whether two simulators produced the same final state."""
  ma, mb = a.get("memory") or [], b.get("memory") or []
  diffs = [i for i in range(spec.memory_words)
           if (ma[i] if i < len(ma) else None) != (mb[i] if i < len(mb) else None)]
  return_equal = (not spec.returns_value) or a.get("return") == b.get("return")
  return {
      "identical": not diffs and return_equal,
      "differing_words": len(diffs),
      "first_differing_addrs": diffs[:MAX_REPORTED_MISMATCHES],
      "return_equal": return_equal,
  }
