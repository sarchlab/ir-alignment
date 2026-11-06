"""Kernel specification loading and memory-image construction.

A kernel spec (``kernels/<name>/kernel.yaml``) describes one end-to-end
alignment case:

* which C/C++ source Neura compiles, with which flags;
* the kernel function signature (pointer and scalar arguments, in order);
* the initial contents of every array argument.

All array arguments are placed in one flat, word-addressed (32-bit) memory
image. Neura emits pointer arguments as symbolic ``argN`` operands; the
pipeline rewrites them to the base address chosen here, and scalar arguments
to their concrete values, so that Zeonica and VectorCGRA consume exactly the
same program and the same memory image.
"""

import dataclasses
import math
from pathlib import Path
from typing import Dict, List, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
KERNELS_DIR = REPO_ROOT / "kernels"

# Gap (in words) left between consecutive arrays so that an off-by-one
# address bug in either simulator shows up as a mismatch instead of
# silently aliasing a neighbouring array.
ARRAY_GUARD_WORDS = 4
# Arrays start on this alignment (in words).
ARRAY_ALIGN_WORDS = 4

INT32_MIN = -(1 << 31)
INT32_MAX = (1 << 31) - 1


def to_int32(value: int) -> int:
  value &= 0xFFFFFFFF
  return value - (1 << 32) if value & 0x80000000 else value


@dataclasses.dataclass
class Arg:
  index: int
  name: str
  kind: str  # "array" or "scalar"
  size: int = 0
  value: int = 0
  base: int = 0
  init: List[int] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class KernelSpec:
  name: str
  directory: Path
  source: Path
  language: str  # "c" or "c++"
  function: str
  extract_regex: str
  cflags: List[str]
  args: List[Arg]
  returns_value: bool
  mapping_options: str
  timeouts: Dict[str, int]
  vectorcgra: Dict
  zeonica: Dict
  description: str
  memory_words: int

  # --- Derived helpers -----------------------------------------------------

  def require_source(self) -> Path:
    if not self.source.exists():
      raise FileNotFoundError(
          f"{self.name}: source not found: {self.source} "
          "(is the zeonica submodule checked out recursively?)")
    return self.source

  @property
  def arrays(self) -> List[Arg]:
    return [a for a in self.args if a.kind == "array"]

  def arg_operand_values(self) -> Dict[str, int]:
    """Maps Neura's symbolic ``argN`` operand names to concrete values."""
    mapping = {}
    for arg in self.args:
      mapping[f"arg{arg.index}"] = arg.base if arg.kind == "array" else arg.value
    return mapping

  def initial_memory(self) -> List[int]:
    mem = [0] * self.memory_words
    for arr in self.arrays:
      mem[arr.base:arr.base + arr.size] = arr.init
    return mem

  def memory_layout(self) -> Dict:
    return {
        "kernel": self.name,
        "memory_words": self.memory_words,
        "returns_value": self.returns_value,
        "args": [
            {
                "index": a.index,
                "name": a.name,
                "kind": a.kind,
                "base": a.base if a.kind == "array" else None,
                "size": a.size if a.kind == "array" else None,
                "value": a.value if a.kind == "scalar" else None,
            }
            for a in self.args
        ],
        "initial_memory": self.initial_memory(),
    }


def _eval_init(expr: str, size: int, kernel: str, arg: str) -> List[int]:
  """Evaluates a per-element init expression over index ``i``."""
  values = []
  env = {"__builtins__": {}, "abs": abs, "min": min, "max": max}
  for i in range(size):
    try:
      v = eval(expr, env, {"i": i, "n": size})  # noqa: S307 - trusted repo data
    except Exception as e:  # pragma: no cover - surfaced to the user
      raise ValueError(f"{kernel}: bad init expression for '{arg}': {expr!r}: {e}")
    values.append(to_int32(int(v)))
  return values


def load_spec(name_or_path: str) -> KernelSpec:
  path = Path(name_or_path)
  if path.is_dir():
    spec_file = path / "kernel.yaml"
  elif path.suffix in (".yaml", ".yml") and path.exists():
    spec_file = path
  else:
    spec_file = KERNELS_DIR / name_or_path / "kernel.yaml"
  if not spec_file.exists():
    raise FileNotFoundError(f"kernel spec not found: {spec_file}")

  raw = yaml.safe_load(spec_file.read_text())
  directory = spec_file.parent
  name = raw.get("name", directory.name)

  source_entry = raw["source"]
  source = (REPO_ROOT / source_entry) if not Path(source_entry).is_absolute() else Path(source_entry)
  if not source.exists():
    # Allow sources that live next to the spec.
    alt = directory / source_entry
    if alt.exists():
      source = alt
  # A missing source only matters for the compile/prepare stages (see
  # KernelSpec.require_source); simulator-only jobs don't check it out.

  language = raw.get("language")
  if language is None:
    language = "c++" if source.suffix in (".cpp", ".cc", ".cxx") else "c"

  args: List[Arg] = []
  for index, entry in enumerate(raw["args"]):
    kind = entry["kind"]
    arg = Arg(index=index, name=entry["name"], kind=kind)
    if kind == "array":
      arg.size = int(entry["size"])
      if "values" in entry:
        vals = [to_int32(int(v)) for v in entry["values"]]
        if len(vals) != arg.size:
          raise ValueError(f"{name}: '{arg.name}' has {len(vals)} values but size {arg.size}")
        arg.init = vals
      else:
        arg.init = _eval_init(str(entry.get("init", "0")), arg.size, name, arg.name)
    elif kind == "scalar":
      arg.value = to_int32(int(entry["value"]))
    else:
      raise ValueError(f"{name}: unknown arg kind {kind!r}")
    args.append(arg)

  # Lay out arrays in one flat memory image.
  cursor = int(raw.get("memory_base", 0))
  for arg in args:
    if arg.kind != "array":
      continue
    cursor = int(math.ceil(cursor / ARRAY_ALIGN_WORDS) * ARRAY_ALIGN_WORDS)
    arg.base = cursor
    cursor += arg.size + ARRAY_GUARD_WORDS
  memory_words = max(cursor, 1)

  timeouts = {"neura": 600, "zeonica": 900, "vectorcgra": 3600}
  timeouts.update(raw.get("timeouts", {}))

  return KernelSpec(
      name=name,
      directory=directory,
      source=source,
      language=language,
      function=raw["function"],
      extract_regex=raw.get("extract_regex", f".*{raw['function']}.*"),
      cflags=[str(f) for f in raw.get("cflags", [])],
      args=args,
      returns_value=bool(raw.get("returns_value", False)),
      mapping_options=raw.get("mapping_options", "mapping-strategy=heuristic"),
      timeouts=timeouts,
      vectorcgra=raw.get("vectorcgra", {}) or {},
      zeonica=raw.get("zeonica", {}) or {},
      description=raw.get("description", ""),
      memory_words=memory_words,
  )


def all_kernel_names() -> List[str]:
  return sorted(p.parent.name for p in KERNELS_DIR.glob("*/kernel.yaml"))
