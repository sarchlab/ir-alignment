"""Drives Neura: C/C++ -> LLVM IR -> MLIR -> mapped Neura IR -> YAML/ASM.

The pass pipeline mirrors the one used by Neura's own end-to-end tests
(``third_party/neura/test/e2e``), so this stage tracks upstream Neura.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import List

from .spec import REPO_ROOT, KernelSpec

NEURA_DIR = REPO_ROOT / "third_party" / "neura"
DEFAULT_ARCH_SPEC = NEURA_DIR / "test" / "arch_spec" / "architecture.yaml"

NEURA_PASSES = [
    "--assign-accelerator",
    "--lower-llvm-to-neura",
    "--promote-input-arg-to-const",
    "--fold-constant",
    "--canonicalize-return",
    "--canonicalize-live-in",
    "--leverage-predicated-value",
    "--transform-ctrl-to-data-flow",
    "--fold-constant",
    "--insert-data-mov",
]


class StageError(RuntimeError):
  pass


def _tool(name: str, env_var: str, search_dirs: List[Path]) -> str:
  override = os.environ.get(env_var)
  if override:
    return override
  for d in search_dirs:
    candidate = d / name
    if candidate.exists():
      return str(candidate)
  found = shutil.which(name)
  if found:
    return found
  raise StageError(f"cannot find '{name}' (set {env_var} or put it on PATH)")


def llvm_bin_dirs() -> List[Path]:
  dirs = []
  if os.environ.get("LLVM_BUILD_DIR"):
    dirs.append(Path(os.environ["LLVM_BUILD_DIR"]) / "bin")
  return dirs


def neura_bin_dirs() -> List[Path]:
  dirs = []
  if os.environ.get("NEURA_BUILD_DIR"):
    dirs.append(Path(os.environ["NEURA_BUILD_DIR"]) / "tools" / "mlir-neura-opt")
  dirs.append(NEURA_DIR / "build" / "tools" / "mlir-neura-opt")
  return dirs


def run_logged(cmd, log, cwd=None, timeout=None):
  log.write("$ " + " ".join(str(c) for c in cmd) + "\n")
  log.flush()
  try:
    proc = subprocess.run([str(c) for c in cmd], cwd=cwd, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, timeout=timeout)
  except subprocess.TimeoutExpired as e:
    out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    log.write(out)
    raise StageError(f"timed out after {timeout}s: {cmd[0]}")
  log.write(proc.stdout)
  log.flush()
  if proc.returncode != 0:
    tail = "\n".join(proc.stdout.strip().splitlines()[-25:])
    raise StageError(f"{Path(str(cmd[0])).name} exited with {proc.returncode}:\n{tail}")
  return proc.stdout


def compile_kernel(spec: KernelSpec, out_dir: Path, arch_spec: Path = None) -> Path:
  """Compiles a kernel with Neura. Returns the generated instruction YAML."""
  out_dir.mkdir(parents=True, exist_ok=True)
  arch_spec = Path(arch_spec or os.environ.get("NEURA_ARCH_SPEC", DEFAULT_ARCH_SPEC))
  clang_name = "clang++" if spec.language == "c++" else "clang"
  clang = _tool(clang_name, "CLANG" if spec.language == "c" else "CLANGXX", llvm_bin_dirs())
  llvm_extract = _tool("llvm-extract", "LLVM_EXTRACT", llvm_bin_dirs())
  mlir_translate = _tool("mlir-translate", "MLIR_TRANSLATE", llvm_bin_dirs())
  neura_opt = _tool("mlir-neura-opt", "MLIR_NEURA_OPT", neura_bin_dirs())
  std = "-std=c++17" if spec.language == "c++" else "-std=c11"

  full_ll = out_dir / "kernel-full.ll"
  only_ll = out_dir / "kernel-only.ll"
  kernel_mlir = out_dir / "kernel.mlir"
  mapping_mlir = out_dir / "mapping.mlir"
  timeout = spec.timeouts["neura"]

  for stale in out_dir.glob("tmp-generated-*"):
    stale.unlink()

  with open(out_dir / "neura.log", "w") as log:
    run_logged([clang, "-S", "-emit-llvm", "-O3", "-fno-vectorize", "-fno-unroll-loops", std,
                f"-I{spec.source.parent}", *spec.cflags, "-o", full_ll, spec.source],
               log, timeout=timeout)
    run_logged([llvm_extract, f"--rfunc={spec.extract_regex}", full_ll, "-S", "-o", only_ll],
               log, timeout=timeout)
    run_logged([mlir_translate, "--import-llvm", only_ll, "-o", kernel_mlir], log, timeout=timeout)
    # --generate-code writes tmp-generated-* into the working directory.
    run_logged([neura_opt, kernel_mlir.name, *NEURA_PASSES,
                f"--map-to-accelerator={spec.mapping_options}",
                f"--architecture-spec={arch_spec}",
                "--generate-code", "-o", mapping_mlir.name],
               log, cwd=out_dir, timeout=timeout)

  yaml_path = out_dir / "tmp-generated-instructions.yaml"
  if not yaml_path.exists():
    raise StageError("mlir-neura-opt did not produce tmp-generated-instructions.yaml")
  return yaml_path
