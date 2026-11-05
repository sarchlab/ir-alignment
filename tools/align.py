#!/usr/bin/env python3
"""ir-alignment driver: Neura -> {Zeonica, VectorCGRA} end-to-end checks.

For every kernel in kernels/<name>/kernel.yaml:

  compile     Neura compiles the C/C++ kernel to mapped CGRA instructions
              (tmp-generated-instructions.yaml).
  prepare     Bind Neura's symbolic argN operands to a concrete memory layout,
              build the initial memory image, and compute the golden result
              by running the kernel source natively on that same image.
  zeonica     Execute the program on Zeonica (tools/zeonica-runner).
  vectorcgra  Execute the program on VectorCGRA's CgraRTL
              (tools/vectorcgra_runner.py).
  check       Compare each simulator's final memory/return value with golden.

Outputs go to build/<kernel>/ (override with --out). Every stage records a
status JSON so `summary` can explain *where* a kernel failed.

Examples:
  tools/align.py run gemm --sims zeonica,vectorcgra
  tools/align.py run --all --sims zeonica
  tools/align.py summary --markdown >> "$GITHUB_STEP_SUMMARY"
"""

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ir_align import compare as cmp  # noqa: E402
from ir_align import golden as gold  # noqa: E402
from ir_align import neura  # noqa: E402
from ir_align import program as prog  # noqa: E402
from ir_align.spec import REPO_ROOT, all_kernel_names, load_spec  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "build"
ZEONICA_ARCH = REPO_ROOT / "arch" / "zeonica.yaml"
SIMULATORS = ("zeonica", "vectorcgra")


def kdir(out_root: Path, name: str) -> Path:
  d = out_root / name
  d.mkdir(parents=True, exist_ok=True)
  return d


def write_json(path: Path, obj):
  path.write_text(json.dumps(obj, indent=2))


def read_json(path: Path, default=None):
  try:
    return json.loads(path.read_text())
  except (FileNotFoundError, json.JSONDecodeError):
    return default


def record_stage(d: Path, stage: str, ok: bool, seconds: float, detail: str = "", extra=None):
  # One file per stage: CI runs stages in different jobs and merges their
  # artifacts, so a shared status file would be overwritten.
  entry = {"ok": ok, "seconds": round(seconds, 2), "detail": detail[-4000:]}
  if extra:
    entry.update(extra)
  write_json(d / f"status_{stage}.json", entry)


def read_status(d: Path) -> dict:
  status = {}
  for f in sorted(d.glob("status_*.json")):
    entry = read_json(f)
    if entry is not None:
      status[f.stem[len("status_"):]] = entry
  return status


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

def stage_compile(spec, d: Path, args) -> bool:
  t = time.time()
  try:
    yaml_path = neura.compile_kernel(spec, d / "neura", args.neura_arch)
    record_stage(d, "compile", True, time.time() - t, str(yaml_path))
    return True
  except Exception as e:  # noqa: BLE001
    record_stage(d, "compile", False, time.time() - t, f"{type(e).__name__}: {e}")
    print(f"[{spec.name}] compile FAILED: {e}", file=sys.stderr)
    return False


def stage_prepare(spec, d: Path, args) -> bool:
  t = time.time()
  try:
    neura_yaml = Path(args.neura_yaml) if args.neura_yaml else d / "neura" / "tmp-generated-instructions.yaml"
    info = prog.patch_program(spec, neura_yaml, d / "program.yaml")
    write_json(d / "memory.json", spec.memory_layout())
    golden = gold.compute_golden(spec, d / "golden")
    write_json(d / "golden.json", golden)
    info["memory_words"] = spec.memory_words
    write_json(d / "program_info.json", info)
    record_stage(d, "prepare", True, time.time() - t,
                 f"II={info['compiled_ii']} opcodes={','.join(info['opcodes'])}", {"info": info})
    return True
  except Exception as e:  # noqa: BLE001
    record_stage(d, "prepare", False, time.time() - t, f"{type(e).__name__}: {e}")
    print(f"[{spec.name}] prepare FAILED: {e}", file=sys.stderr)
    traceback.print_exc()
    return False


def _run_tool(cmd, log_path: Path, timeout: int, env=None):
  """Runs a simulator subprocess, streaming its output into a log file."""
  with open(log_path, "w") as log:
    log.write("$ " + " ".join(str(c) for c in cmd) + "\n")
    log.flush()
    try:
      proc = subprocess.run([str(c) for c in cmd], stdout=log, stderr=subprocess.STDOUT,
                            timeout=timeout, env=env)
      return proc.returncode, False
    except subprocess.TimeoutExpired:
      log.write(f"\n*** ir-alignment: killed after wall-clock timeout of {timeout}s ***\n")
      return None, True


def _log_tail(path: Path, n=30) -> str:
  try:
    return "\n".join(path.read_text(errors="replace").splitlines()[-n:])
  except FileNotFoundError:
    return ""


def stage_zeonica(spec, d: Path, args) -> bool:
  t = time.time()
  runner = args.zeonica_runner or os.environ.get("ZEONICA_RUNNER") or str(REPO_ROOT / "build" / "bin" / "zeonica-runner")
  out = d / "zeonica.json"
  out.unlink(missing_ok=True)
  timeout = int(spec.zeonica.get("timeout", spec.timeouts["zeonica"]) * args.timeout_scale)
  rc, killed = _run_tool([runner, "-program", d / "program.yaml", "-memory", d / "memory.json",
                          "-arch", args.zeonica_arch, "-out", out], d / "zeonica.log", timeout)
  ok = rc == 0 and out.exists()
  if ok:
    detail = ""
  elif killed:
    detail = f"wall-clock timeout ({timeout}s)"
  else:
    tail = _log_tail(d / "zeonica.log", 400)
    panics = [l for l in tail.splitlines() if l.startswith(("panic:", "zeonica-runner:", "fatal error:"))]
    detail = (panics[0] if panics else f"exit code {rc}") + "\n" + "\n".join(tail.splitlines()[-30:])
  record_stage(d, "zeonica", ok, time.time() - t, detail, {"timeout": timeout})
  if not ok:
    print(f"[{spec.name}] zeonica FAILED: {detail.splitlines()[0] if detail else ''}", file=sys.stderr)
  return ok


def stage_vectorcgra(spec, d: Path, args) -> bool:
  t = time.time()
  out = d / "vectorcgra.json"
  out.unlink(missing_ok=True)
  vcfg = spec.vectorcgra
  backend = args.vectorcgra_backend or vcfg.get("backend", "auto")
  timeout = int(vcfg.get("timeout", spec.timeouts["vectorcgra"]) * args.timeout_scale)
  max_cycles = int(vcfg.get("max_cycles", 200000))
  python = args.vectorcgra_python or os.environ.get("VECTORCGRA_PYTHON") or sys.executable
  # Verilator builds land in the working directory. All kernels share one
  # directory (and one fixed CgraRTL configuration) so the Verilated model is
  # compiled once and reused from pymtl3's import cache.
  workdir = Path(args.vectorcgra_workdir or (Path(args.out) / "_vectorcgra_model"))
  workdir.mkdir(parents=True, exist_ok=True)
  cmd = [python, REPO_ROOT / "tools" / "vectorcgra_runner.py",
         "--program", d / "program.yaml", "--memory", d / "memory.json", "--out", out,
         "--backend", backend, "--max-cycles", max_cycles]
  log = d / "vectorcgra.log"
  with open(log, "w") as f:
    f.write("$ " + " ".join(str(c) for c in cmd) + "\n")
  try:
    with open(log, "a") as f:
      proc = subprocess.run([str(c) for c in cmd], stdout=f, stderr=subprocess.STDOUT,
                            timeout=timeout, cwd=workdir)
    rc, killed = proc.returncode, False
  except subprocess.TimeoutExpired:
    with open(log, "a") as f:
      f.write(f"\n*** ir-alignment: killed after wall-clock timeout of {timeout}s ***\n")
    rc, killed = None, True
  res = read_json(out)
  ok = res is not None and rc in (0, 3)  # 3 = simulated-cycle timeout (reported by check)
  translate_err = [l for l in _log_tail(log, 400).splitlines() if l.startswith("vectorcgra: TRANSLATION FAILED")]
  if translate_err:
    detail = translate_err[-1].replace("vectorcgra: ", "")
  elif killed:
    detail = f"wall-clock timeout ({timeout}s)\n" + _log_tail(log, 8)
  elif res is None:
    detail = f"exit code {rc}\n" + _log_tail(log)
  elif res.get("timed_out"):
    detail = f"no RETURN within {max_cycles} cycles (deadlock/stall?)"
  else:
    detail = ""
  record_stage(d, "vectorcgra", ok and not (res or {}).get("timed_out"), time.time() - t, detail,
               {"timeout": timeout, "backend": backend})
  if detail:
    print(f"[{spec.name}] vectorcgra: {detail.splitlines()[0]}", file=sys.stderr)
  return ok


def stage_check(spec, d: Path, sims) -> bool:
  golden = read_json(d / "golden.json")
  report = {"kernel": spec.name, "description": spec.description, "simulators": {}}
  all_ok = golden is not None
  for sim in sims:
    res = read_json(d / f"{sim}.json")
    if golden is None or res is None:
      report["simulators"][sim] = {"passed": False, "error": "no result (see status_*.json / logs)"}
      all_ok = False
      continue
    r = cmp.compare(spec, golden, res)
    report["simulators"][sim] = r
    all_ok = all_ok and r["passed"]
  # Cross-simulator agreement: even when Neura's IR does not implement the
  # source semantics, both consumers of the same IR should agree.
  results = {sim: read_json(d / f"{sim}.json") for sim in SIMULATORS}
  if all(results.values()):
    report["agreement"] = cmp.agreement(spec, results["zeonica"], results["vectorcgra"])
  report["passed"] = all_ok
  write_json(d / "report.json", report)
  return all_ok


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_run(args) -> int:
  names = all_kernel_names() if args.all or not args.kernels else args.kernels
  sims = [s for s in args.sims.split(",") if s]
  for s in sims:
    if s not in SIMULATORS:
      raise SystemExit(f"unknown simulator {s!r}; choose from {SIMULATORS}")
  stages = args.stages.split(",")
  out_root = Path(args.out)
  failures = []
  for name in names:
    spec = load_spec(name)
    d = kdir(out_root, spec.name)
    print(f"=== {spec.name} ===", flush=True)
    ok = True
    if "prepare" not in stages and not (d / "program.yaml").exists():
      print(f"[{spec.name}] no prepared program (Neura compile/prepare failed); skipping", file=sys.stderr)
      ok = False
    if "compile" in stages and not args.neura_yaml:
      ok = stage_compile(spec, d, args)
    if ok and "prepare" in stages:
      ok = stage_prepare(spec, d, args)
    if ok:
      for sim in sims:
        if sim in stages or "sim" in stages:
          (stage_zeonica if sim == "zeonica" else stage_vectorcgra)(spec, d, args)
    passed = stage_check(spec, d, sims) if ok else False
    if not ok:
      write_json(d / "report.json", {
          "kernel": spec.name, "passed": False, "description": spec.description,
          "simulators": {sim: {"passed": False, "error": "Neura compile/prepare failed"} for sim in sims}})
    rep = read_json(d / "report.json", {})
    for sim, r in rep.get("simulators", {}).items():
      tag = "PASS" if r.get("passed") else "FAIL"
      extra = f"cycles={r.get('cycles')}" if r.get("passed") else (
          r.get("error") or f"{r.get('mismatched_words')} wrong words" +
          (f", return {r['return']['got']} != {r['return']['expected']}" if r.get("return") and not r["return"]["ok"] else ""))
      print(f"  {sim:<11} {tag}  {extra}", flush=True)
    if not passed:
      failures.append(spec.name)
  verb = "passed" if sims else "compiled and prepared"
  print(f"\n{len(names) - len(failures)}/{len(names)} kernels {verb}" +
        (f"; failed: {', '.join(failures)}" if failures else ""))
  return 1 if failures else 0


def cmd_check(args) -> int:
  """Recomputes report.json from results already in --out (e.g. downloaded CI artifacts)."""
  sims = [x for x in args.sims.split(",") if x]
  out_root = Path(args.out)
  for name in all_kernel_names():
    d = out_root / name
    if not d.exists():
      continue
    stage_check(load_spec(name), d, sims)
  return 0


def load_expected():
  path = REPO_ROOT / "expected.yaml"
  if not path.exists():
    return {}
  import yaml
  return yaml.safe_load(path.read_text()) or {}


def _cell(status, report, sim):
  st = (status or {}).get(sim)
  r = (report or {}).get("simulators", {}).get(sim)
  if st is None and r is None:
    return "–"
  if r and r.get("passed"):
    return f"✅ {r.get('cycles')} cyc"
  if st and not st.get("ok"):
    first = (st.get("detail") or "failed").strip().splitlines()[0][:80]
    return f"❌ {first}"
  if r:
    if r.get("timed_out"):
      return "❌ timeout"
    bits = []
    if r.get("mismatched_words"):
      bits.append(f"{r['mismatched_words']} wrong words")
    if r.get("return") and not r["return"]["ok"]:
      bits.append(f"ret {r['return']['got']}≠{r['return']['expected']}")
    if r.get("stray_writes"):
      bits.append("stray writes")
    return "❌ " + (", ".join(bits) or r.get("error", "failed"))
  return "❌"


def _outcome(status, report, sim):
  r = (report or {}).get("simulators", {}).get(sim)
  st = (status or {}).get(sim)
  if r is None and st is None:
    return None
  return "pass" if (r or {}).get("passed") else "fail"


def _agree_cell(report):
  a = (report or {}).get("agreement")
  if not a:
    return "–"
  if a["identical"]:
    return "✅ identical"
  bits = []
  if a["differing_words"]:
    bits.append(f"{a['differing_words']} words differ")
  if not a["return_equal"]:
    bits.append("return differs")
  return "❌ " + ", ".join(bits)


def cmd_summary(args) -> int:
  out_root = Path(args.out)
  expected = load_expected()
  sims = [x for x in args.sims.split(",") if x]
  rows = []
  regressions, xpasses, missing = [], [], []
  for name in all_kernel_names():
    d = out_root / name
    status = read_status(d) if d.exists() else {}
    report = read_json(d / "report.json", {}) if d.exists() else {}
    prep = status.get("prepare", {})
    ii = (prep.get("info") or {}).get("compiled_ii", "")
    comp = status.get("compile")
    if comp is None:
      neura_cell = "–"
    elif comp.get("ok"):
      neura_cell = "✅"
    else:
      neura_cell = "❌ " + ((comp.get("detail") or "").splitlines() or [""])[0][:80]
    if prep and not prep.get("ok"):
      neura_cell += " / prepare ❌ " + ((prep.get("detail") or "").splitlines() or [""])[0][:80]
    exp = expected.get(name, {}) or {}
    cells = []
    for sim in sims:
      outcome = _outcome(status, report, sim)
      want = exp.get(sim, "pass")
      cell = _cell(status, report, sim)
      if outcome is None:
        missing.append(f"{name}/{sim}")
      elif outcome == "fail" and want == "pass":
        regressions.append(f"{name}/{sim}")
        cell += " **(unexpected)**"
      elif outcome == "fail":
        cell += " *(known)*"
      elif outcome == "pass" and want == "fail":
        xpasses.append(f"{name}/{sim}")
        cell += " **(now passes!)**"
      cells.append(cell)
    rows.append([name, neura_cell, str(ii), *cells, _agree_cell(report)])

  header = ["kernel", "neura", "II", *sims, "zeonica ≡ vectorcgra"]
  if args.markdown:
    print("| " + " | ".join(header) + " |")
    print("|" + "---|" * len(header))
    for r in rows:
      print("| " + " | ".join(c.replace("|", "\\|") for c in r) + " |")
    print()
    if regressions:
      print("**Unexpected failures (not in `expected.yaml`):** " + ", ".join(regressions))
    if xpasses:
      print("**Known failures that now pass** – remove them from `expected.yaml`: " + ", ".join(xpasses))
    if missing:
      print("**Missing results:** " + ", ".join(missing))
    reasons = {k: v.get("reason") for k, v in expected.items() if isinstance(v, dict) and v.get("reason")}
    if reasons:
      print("\n<details><summary>Known failures (expected.yaml)</summary>\n")
      for k, why in reasons.items():
        print(f"- **{k}**: {why}")
      print("\n</details>")
  else:
    for r in rows:
      print("  ".join(f"{c:<12}" if i < 3 else c for i, c in enumerate(r)))
    if regressions:
      print("unexpected failures: " + ", ".join(regressions))
    if xpasses:
      print("known failures that now pass: " + ", ".join(xpasses))
    if missing:
      print("missing results: " + ", ".join(missing))

  if args.gate:
    bad = regressions + (missing if args.require_all else [])
    if args.strict_xpass:
      bad += xpasses
    return 1 if bad else 0
  return 0


def main():
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  sub = ap.add_subparsers(dest="cmd", required=True)

  run = sub.add_parser("run", help="run the pipeline for kernels")
  run.add_argument("kernels", nargs="*")
  run.add_argument("--all", action="store_true")
  run.add_argument("--sims", default="zeonica,vectorcgra")
  run.add_argument("--stages", default="compile,prepare,sim",
                   help="comma list of compile,prepare,sim (sim = run the selected --sims)")
  run.add_argument("--out", default=str(DEFAULT_OUT))
  run.add_argument("--neura-yaml", help="skip compile and use this Neura YAML instead")
  run.add_argument("--neura-arch", default=None, help="Neura architecture spec override")
  run.add_argument("--zeonica-arch", default=str(ZEONICA_ARCH))
  run.add_argument("--zeonica-runner", default=None)
  run.add_argument("--vectorcgra-python", default=None)
  run.add_argument("--vectorcgra-backend", choices=["auto", "sim", "verilator"], default=None,
                   help="override the per-kernel backend from kernel.yaml")
  run.add_argument("--vectorcgra-workdir", default=None,
                   help="shared working dir for the Verilated model (default: <out>/_vectorcgra_model)")
  run.add_argument("--timeout-scale", type=float, default=float(os.environ.get("ALIGN_TIMEOUT_SCALE", "1")))
  run.set_defaults(func=cmd_run)

  chk = sub.add_parser("check", help="recompute build/<kernel>/report.json from existing results")
  chk.add_argument("--out", default=str(DEFAULT_OUT))
  chk.add_argument("--sims", default="zeonica,vectorcgra")
  chk.set_defaults(func=cmd_check)

  summ = sub.add_parser("summary", help="summarise build/<kernel>/report.json against expected.yaml")
  summ.add_argument("--out", default=str(DEFAULT_OUT))
  summ.add_argument("--sims", default="zeonica,vectorcgra")
  summ.add_argument("--markdown", action="store_true")
  summ.add_argument("--gate", action="store_true",
                    help="exit 1 on any result worse than expected.yaml (a regression)")
  summ.add_argument("--require-all", action="store_true",
                    help="with --gate, also fail when a kernel/simulator result is missing")
  summ.add_argument("--strict-xpass", action="store_true",
                    help="with --gate, also fail when a known failure starts passing")
  summ.set_defaults(func=cmd_summary)

  lst = sub.add_parser("list", help="list kernels (JSON array, for CI matrices)")
  lst.set_defaults(func=lambda a: print(json.dumps(all_kernel_names())) or 0)

  args = ap.parse_args()
  sys.exit(args.func(args))


if __name__ == "__main__":
  main()
