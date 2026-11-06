# ir-alignment

End-to-end alignment tests for the CGRA toolchain:

```
            C/C++ kernel
                 │  clang -O3 → llvm-extract → mlir-translate
                 ▼
  neura (mlir-neura-opt) ── mapped dataflow IR ──► tmp-generated-instructions.yaml
                 │
                 │  argN → concrete addresses/values, one shared memory image
                 ▼
        ┌────────┴─────────┐
        ▼                  ▼
     zeonica          VectorCGRA (CgraRTL)
        │                  │
        └──── final memory image + return value ────┐
                                                    ▼
                 compared with the kernel source run natively (golden)
```

The three tools are git submodules under `third_party/`:

| submodule | repo | tracked branch |
|---|---|---|
| `third_party/neura` | [coredac/neura](https://github.com/coredac/neura) | `main` |
| `third_party/zeonica` | [sarchlab/zeonica](https://github.com/sarchlab/zeonica) | `main` |
| `third_party/VectorCGRA` | [tancheng/VectorCGRA](https://github.com/tancheng/VectorCGRA) | `master` |

## Kernels

Every case from Zeonica's testbench (`Zeonica_Testbench/kernel/*`) is covered.
Each lives in `kernels/<name>/kernel.yaml`:

| kernel | source | what it exercises |
|---|---|---|
| axpy | Zeonica_Testbench `axpy_int.cpp` | streaming load/mul/add/store |
| gemm | Zeonica_Testbench `gemm_int.c` (NI=4, NJ=6, NK=5) | 3-deep nest, 2-D arrays, scalar args |
| gemv | Zeonica_Testbench `gemv_int.cpp` | 2-deep nest, reduction |
| fir | Zeonica_Testbench `fir_int.cpp` | reduction returned by value |
| spmv | Zeonica_Testbench `spmv.c` (nnz=24) | indirect loads, scattered stores, unroll-by-4 |
| conv | `kernels/conv/conv2d_int.c` | 4-deep nest (6×6 image, 3×3 filter) |
| relu | Zeonica_Testbench `relu_int.cpp` | predicated select |
| bicg | Zeonica_Testbench `bicg_int.c` | two reductions sharing loads |
| histogram | Zeonica_Testbench `histogram_int.cpp` | divide, data-dependent read-modify-write |
| jacobi2d | `kernels/jacobi2d/jacobi2d_int.c` | 5-point stencil |
| fft | Zeonica_Testbench `fft_int.c` | 256-point, 3-deep nest, 1024 butterflies |

Zeonica's testbench only ships hand-mapped assembly for conv and jacobi2d, so
small integer C versions live in this repo and Neura compiles them like the
others. Array contents are chosen per kernel (non-square shapes, negative
values, distinct values per array) so address/stride mistakes cannot hide.

A kernel passes on a simulator when **every word** of every array (plus the
guard words between arrays, to catch stray stores) and the return value match
the golden run. The report also shows whether Zeonica and VectorCGRA agree
with *each other*, which separates simulator mismatches from IR bugs.

## How it works

| stage | tool | notes |
|---|---|---|
| compile | `tools/ir_align/neura.py` | the same pass pipeline as `neura/test/e2e`, using neura's own `test/arch_spec/architecture.yaml` |
| prepare | `tools/ir_align/program.py`, `golden.py` | lays the arrays out in one word-addressed memory, rewrites `argN` operands to `#base`/`#value`, compiles the kernel source into a native harness (`#include`d, `-fwrapv`) to get the golden image |
| zeonica | `tools/zeonica-runner` (Go) | `arch/zeonica.yaml`: 4×4 mesh, banked shared SRAM with 1-cycle latency (what Neura's schedule assumes) |
| vectorcgra | `tools/vectorcgra_runner.py` | VectorCGRA's own `validation/script_generator.py` turns the YAML into config packets; memory is preloaded/read back over the CPU port; runs until the kernel's `RETURN` (no control-step limit) |
| check | `tools/ir_align/compare.py` | per-array mismatches, stray writes, return value, cross-simulator agreement |

VectorCGRA's pure-Python simulation runs ~1 cycle/s for a 4×4 CgraRTL, so
kernels run on a Verilator build of CgraRTL (~140 cycles/s, ~20 min to build
once). All kernels share one fixed CgraRTL configuration so the model is
built once per VectorCGRA commit and reused (CI caches it). Upstream `DivRTL`
is not Verilog-translatable (`//`), so the Verilator model uses
`tools/vcgra_ext/TranslatableDivRTL.py`: DivRTL's control logic verbatim
(enforced by `check_divrtl_sync.py`) with a restoring divider verified
bit-exactly against Python (`test_udiv.py`).

## Running locally

Prerequisites: an LLVM build at neura's pinned commit (`scripts/build_llvm.sh`),
Go ≥ 1.24, Python 3.11 for neura's build and a Python with VectorCGRA's
requirements (`scripts/setup_vectorcgra.sh`), Verilator.

```bash
git submodule update --init --recursive      # see scripts/checkout_refs.sh
scripts/fetch_llvm.sh ~/llvm-project         # prebuilt release (needs gh), or:
# scripts/build_llvm.sh ~/llvm-project       # from source; ~30 min on 40 cores
LLVM_BUILD_DIR=~/llvm-project/build scripts/build_neura.sh
(cd tools/zeonica-runner && go build -o ../../build/bin/zeonica-runner .)

export LLVM_BUILD_DIR=~/llvm-project/build
python tools/align.py run --all                      # everything
python tools/align.py run gemm fir --sims zeonica    # a subset
python tools/align.py summary                        # table of build/*/report.json
```

Outputs per kernel are in `build/<kernel>/`: Neura's IR and YAML
(`neura/`), the concrete `program.yaml`, `memory.json`, `golden.json`,
each simulator's result/log, and `report.json`.

## CI

`.github/workflows/alignment.yml` runs on pushes and PRs (pinned
submodule commits), every 3 hours against the **latest** upstream commits
(skipped when that exact combination already passed), on
`repository_dispatch` from the upstream repos, and manually with arbitrary
refs. See [docs/upstream-trigger.md](docs/upstream-trigger.md).

Jobs: `neura → IR` → `zeonica` and one `vectorcgra / <kernel>` job per
kernel (in parallel, with the Verilated model built by `vectorcgra model`)
→ `report`. The report job writes a table to the run summary and fails on any
result worse than `expected.yaml`.

### Keeping runs fast

| what | how | typical cost |
|---|---|---|
| LLVM/MLIR | prebuilt, trimmed package (`scripts/package_llvm.sh`, 334 MB) downloaded from this repo's release `llvm-<commit>` by `scripts/fetch_llvm.sh` | ~1 min |
| neura | built against it with ccache | ~2–8 min |
| kernel compilation | all kernels in parallel (`align.py run -j`) | ~4 min (spmv mapping dominates) |
| VectorCGRA model | Verilated once per VectorCGRA commit, cached; runs alongside neura | 20 min on a new commit, else ~1 min |
| simulation | one job per kernel, in parallel | minutes each |

A typical run takes about 20–30 minutes. LLVM is only built from source when
neura moves to a new LLVM commit and no `llvm-<commit>` release exists yet;
that run (~3–4 h) publishes the package so every later run downloads it.

### Timeouts

| limit | where | value |
|---|---|---|
| simulated cycles before a VectorCGRA run counts as hung | `kernels/*/kernel.yaml` `vectorcgra.max_cycles` | ~10× the expected run (8k … 600k for fft) |
| wall clock per VectorCGRA kernel | `vectorcgra.timeout` | 30–50 min, 4 h for fft |
| wall clock per Zeonica kernel | `zeonica.timeout` | 15 min, 1 h for fft |
| Neura compile per kernel | `timeouts.neura` | 10 min |
| GitHub job limits | workflow `timeout-minutes` | 350 min for the neura job (only reached when LLVM must be built from source), 355 per VectorCGRA kernel |

### Known failures

`expected.yaml` lists (kernel, simulator) pairs that currently fail because of
a known upstream issue, with the reason. The gate fails when anything else
fails, so the CI stays green while still catching every regression. When a
known failure starts passing the summary says so; remove it from
`expected.yaml` (or run `summary --gate --strict-xpass` to enforce that).
