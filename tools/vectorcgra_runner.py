#!/usr/bin/env python3
"""Runs a Neura-generated (argument-patched) kernel on VectorCGRA's CgraRTL.

Flow:
  1. Preload the shared memory image with CMD_STORE_REQUEST packets.
  2. Translate the YAML program to per-tile CMD_CONFIG/CMD_CONST/... packets
     with VectorCGRA's own validation/script_generator.py and launch it.
  3. Wait for the CMD_COMPLETE emitted by the kernel's RETURN, let in-flight
     stores drain, then read the whole memory image back with
     CMD_LOAD_REQUEST packets.
  4. Dump memory + return value + cycle count as JSON.

The kernel runs with an unbounded control-step budget
(CMD_CONFIG_TOTAL_CTRL_COUNT = 0), so the only way to finish is the kernel's
own RETURN firing; a hang is reported as a timeout, not silently truncated.

Usage:
  vectorcgra_runner.py --program P.yaml --memory layout.json --out result.json
                       [--backend sim|verilator] [--max-cycles N]
"""

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
THIRD_PARTY = REPO_ROOT / "third_party"
VCGRA_ROOT = THIRD_PARTY / "VectorCGRA"
# VectorCGRA uses package-relative imports; import it as the `VectorCGRA`
# package from third_party/. script_generator.py additionally puts the repo
# root itself on sys.path for its `lib.opt_type` import.
sys.path.insert(0, str(THIRD_PARTY))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import yaml  # noqa: E402
from pymtl3 import *  # noqa: E402,F401,F403
from pymtl3.passes.backends.verilog import VerilogVerilatorImportPass  # noqa: E402
from pymtl3.passes.sim.PrepareSimPass import b1  # noqa: E402
from pymtl3.stdlib.test_utils import config_model_with_cmdline_opts  # noqa: E402
from pymtl3.stdlib.test_utils.test_helpers import finalize_verilator  # noqa: E402

from VectorCGRA.cgra.CgraRTL import CgraRTL  # noqa: E402
from VectorCGRA.fu.flexible.FlexibleFuRTL import FlexibleFuRTL  # noqa: E402
from VectorCGRA.fu.single.AdderRTL import AdderRTL  # noqa: E402
from VectorCGRA.fu.single.CompRTL import CompRTL  # noqa: E402
from VectorCGRA.fu.single.ConstRTL import ConstRTL  # noqa: E402
from VectorCGRA.fu.single.DivRTL import DivRTL  # noqa: E402
from VectorCGRA.fu.single.GepRTL import GepRTL  # noqa: E402
from VectorCGRA.fu.single.GrantRTL import GrantRTL  # noqa: E402
from VectorCGRA.fu.single.LogicRTL import LogicRTL  # noqa: E402
from VectorCGRA.fu.single.MemUnitRTL import MemUnitRTL  # noqa: E402
from VectorCGRA.fu.single.MulRTL import MulRTL  # noqa: E402
from VectorCGRA.fu.single.PhiRTL import PhiRTL  # noqa: E402
from VectorCGRA.fu.single.RetRTL import RetRTL  # noqa: E402
from VectorCGRA.fu.single.SelRTL import SelRTL  # noqa: E402
from VectorCGRA.fu.single.ShifterRTL import ShifterRTL  # noqa: E402
from VectorCGRA.lib.basic.val_rdy.ifcs import RecvIfcRTL  # noqa: E402
from VectorCGRA.lib.basic.val_rdy.SourceRTL import SourceRTL  # noqa: E402
from VectorCGRA.lib.cmd_type import *  # noqa: E402,F401,F403
from VectorCGRA.lib.messages import *  # noqa: E402,F401,F403
from VectorCGRA.lib.util.common import *  # noqa: E402,F401,F403
from VectorCGRA.validation.script_generator import ScriptFactory  # noqa: E402

from vcgra_ext.TranslatableDivRTL import TranslatableDivRTL  # noqa: E402

# Union of the FUs used by VectorCGRA's own *_test_from_yaml.py tests, so
# every opcode Neura can emit for these kernels has a functional unit.
#
# Divider: upstream DivRTL computes `a // b`, which pymtl3 cannot translate
# to Verilog. The Verilator backend therefore uses TranslatableDivRTL -- the
# same control logic (kept in sync by tools/vcgra_ext/check_divrtl_sync.py)
# with a translatable restoring divider -- and the pure-Python backend uses
# upstream DivRTL itself.
BASE_FUS = [AdderRTL, MulRTL, LogicRTL, ShifterRTL, PhiRTL, ConstRTL,
            CompRTL, GrantRTL, MemUnitRTL, SelRTL, RetRTL]

# GepRTL is a simulation-only model (it builds DataType(0, 0) with 2 of 4
# fields, which the translator rejects). script_generator maps plain GEP to
# the adder; only GEP_2D needs GepRTL, and such programs fall back to the
# pure-Python simulator under --backend auto.
SIM_ONLY_FUS = [({"GEP_2D"}, GepRTL)]


def make_fu_list(backend, opcodes):
  fus = list(BASE_FUS) + [TranslatableDivRTL if backend == "verilator" else DivRTL]
  for trigger, fu in SIM_ONLY_FUS:
    if opcodes & trigger:
      fus.append(fu)
  return fus


def to_int32(v):
  v &= 0xFFFFFFFF
  return v - (1 << 32) if v & 0x80000000 else v


#-------------------------------------------------------------------------
# Collector: always-ready sink that records every packet sent to the CPU.
#-------------------------------------------------------------------------

class Collector(Component):

  def construct(s, PktType):
    s.recv = RecvIfcRTL(PktType)
    s.recv.rdy //= 1
    s.records = []
    s.complete_seen = OutPort(b1)
    s.num_load_responses = 0

    @update_ff
    def collect():
      if s.reset:
        s.complete_seen <<= 0
      elif s.recv.val:
        payload = s.recv.msg.payload
        cmd = int(payload.cmd)
        rec = {
            "cmd": cmd,
            "src": int(s.recv.msg.src),
            "data": int(payload.data.payload),
            "predicate": int(payload.data.predicate),
            "addr": int(payload.data_addr),
        }
        s.records.append(rec)
        if cmd == CMD_COMPLETE:
          s.complete_seen <<= 1
        elif cmd == CMD_LOAD_RESPONSE:
          s.num_load_responses += 1


#-------------------------------------------------------------------------
# Harness
#-------------------------------------------------------------------------

class Harness(Component):

  def construct(s, cfg, ctrl_pkts, query_pkts, drain_cycles):
    CtrlPktType = cfg["IntraCgraPktType"]
    CgraPayloadType = cfg["CgraPayloadType"]
    DataType = cfg["DataType"]
    DataAddrType = cfg["DataAddrType"]
    width, height = cfg["width"], cfg["height"]

    s.src_ctrl = SourceRTL(CtrlPktType, ctrl_pkts)
    s.src_query = SourceRTL(CtrlPktType, query_pkts)
    s.collector = Collector(CtrlPktType)
    s.num_queries = len(query_pkts)

    s.dut = CgraRTL(CgraPayloadType,
                    cfg["num_cgra_rows"], cfg["num_cgra_columns"],
                    width, height, cfg["ctrl_mem_size"],
                    cfg["data_mem_size_global"], cfg["data_mem_size_per_bank"],
                    cfg["num_banks_per_cgra"], cfg["num_registers_per_reg_bank"],
                    # ctrl_count_per_iter only sizes the per-iteration counter
                    # here (the real II is sent at runtime with
                    # CMD_CONFIG_COUNT_PER_ITER); keep it fixed so the
                    # generated Verilog -- and the Verilator build -- is
                    # shared by all kernels.
                    cfg["ctrl_mem_size"],
                    0,  # unbounded total ctrl steps: the kernel's RETURN ends it
                    True,  # combinational memory access
                    FlexibleFuRTL, cfg["fu_list"], "Mesh",
                    cfg["controller2addr_map"], cfg["idTo2d_map"],
                    is_multi_cgra=False)

    s.dut.cgra_id //= 0
    s.collector.recv //= s.dut.send_to_cpu_pkt
    s.dut.address_lower //= DataAddrType(cfg["controller2addr_map"][0][0])
    s.dut.address_upper //= DataAddrType(cfg["controller2addr_map"][0][1])

    for col in range(width):
      for side in ("north", "south"):
        getattr(s.dut, f"send_data_on_boundary_{side}")[col].rdy //= 0
        getattr(s.dut, f"recv_data_on_boundary_{side}")[col].val //= 0
        getattr(s.dut, f"recv_data_on_boundary_{side}")[col].msg //= DataType()
    for row in range(height):
      for side in ("west", "east"):
        getattr(s.dut, f"send_data_on_boundary_{side}")[row].rdy //= 0
        getattr(s.dut, f"recv_data_on_boundary_{side}")[row].val //= 0
        getattr(s.dut, f"recv_data_on_boundary_{side}")[row].msg //= DataType()

    # Count cycles since the kernel's RETURN so in-flight stores can retire
    # before memory is read back.
    s.drain_cycles = drain_cycles
    DrainType = mk_bits(max(1, clog2(drain_cycles + 2)))
    s.drain = Wire(DrainType)

    @update_ff
    def drain_counter():
      if s.reset:
        s.drain <<= 0
      elif s.collector.complete_seen & (s.drain < drain_cycles):
        s.drain <<= s.drain + 1

    @update
    def mux_cpu_pkt():
      s.dut.recv_from_cpu_pkt.val @= s.src_ctrl.send.val
      s.dut.recv_from_cpu_pkt.msg @= s.src_ctrl.send.msg
      s.src_ctrl.send.rdy @= s.dut.recv_from_cpu_pkt.rdy
      s.src_query.send.rdy @= 0
      if s.collector.complete_seen & (s.drain >= drain_cycles) & ~s.src_ctrl.send.val:
        s.dut.recv_from_cpu_pkt.val @= s.src_query.send.val
        s.dut.recv_from_cpu_pkt.msg @= s.src_query.send.msg
        s.src_query.send.rdy @= s.dut.recv_from_cpu_pkt.rdy
        s.src_ctrl.send.rdy @= 0

  def done(s):
    return (s.src_ctrl.done() and s.src_query.done()
            and s.collector.num_load_responses >= s.num_queries)


#-------------------------------------------------------------------------
# Configuration
#-------------------------------------------------------------------------

def next_pow2(n):
  return 1 << max(0, math.ceil(math.log2(max(n, 1))))


def build_config(program, memory_words, args):
  arr = program["array_config"]
  width, height, ii = int(arr["columns"]), int(arr["rows"]), int(arr["compiled_ii"])
  num_registers_per_reg_bank = args.registers_per_bank
  num_cgra_columns, num_cgra_rows = 4, 1
  num_cgras = num_cgra_columns * num_cgra_rows
  num_banks_per_cgra = 2
  # Model parameters are fixed (not derived from the kernel) so that one
  # translated/Verilated CgraRTL is shared by every kernel; pymtl3 reuses
  # the cached Verilator build when the generated Verilog hash matches.
  data_words = max(args.data_mem_words, next_pow2(memory_words))
  data_mem_size_per_bank = data_words // num_banks_per_cgra
  per_cgra = data_mem_size_per_bank * num_banks_per_cgra
  data_mem_size_global = per_cgra * num_cgras
  # The ctrl memory must hold all II ctrl words (CtrlAddrType is sized from
  # it); Neura's architecture spec caps ctrl memory at 20 items.
  ctrl_mem_size = max(args.ctrl_mem_size, next_pow2(ii + 1))

  num_tile_inports = num_tile_outports = 4
  num_fu_inports, num_fu_outports = 4, 2
  num_tiles = width * height
  DataType = mk_data(32, 1)
  DataAddrType = mk_bits(clog2(data_mem_size_global))
  RegIdxType = mk_bits(clog2(num_registers_per_reg_bank))
  CtrlType = mk_ctrl(num_fu_inports, num_fu_outports, num_tile_inports,
                     num_tile_outports, num_registers_per_reg_bank)
  CtrlAddrType = mk_bits(clog2(ctrl_mem_size))
  CgraPayloadType = mk_cgra_payload(DataType, DataAddrType, CtrlType, CtrlAddrType)
  IntraCgraPktType = mk_intra_cgra_pkt(num_cgra_columns, num_cgra_rows, num_tiles, CgraPayloadType)

  opcodes = {op["opcode"]
             for core in arr["cores"] for entry in core["entries"]
             for inst in entry["instructions"] for op in inst["operations"]}
  untranslatable = [f"{fu.__name__} (for {', '.join(sorted(opcodes & trigger))})"
                    for trigger, fu in SIM_ONLY_FUS if opcodes & trigger]

  return {
      "opcodes": opcodes,
      "fu_list": make_fu_list("verilator", opcodes),
      "untranslatable": untranslatable,
      "width": width, "height": height, "ii": ii,
      "num_cgra_columns": num_cgra_columns, "num_cgra_rows": num_cgra_rows,
      "ctrl_mem_size": ctrl_mem_size,
      "data_mem_size_global": data_mem_size_global,
      "data_mem_size_per_bank": data_mem_size_per_bank,
      "num_banks_per_cgra": num_banks_per_cgra,
      "num_registers_per_reg_bank": num_registers_per_reg_bank,
      "controller2addr_map": {i: [i * per_cgra, (i + 1) * per_cgra - 1] for i in range(num_cgras)},
      "idTo2d_map": {i: [i % num_cgra_columns, i // num_cgra_columns] for i in range(num_cgras)},
      "DataType": DataType, "DataAddrType": DataAddrType, "RegIdxType": RegIdxType,
      "CtrlType": CtrlType, "CtrlAddrType": CtrlAddrType,
      "CgraPayloadType": CgraPayloadType, "IntraCgraPktType": IntraCgraPktType,
      "TileInType": mk_bits(clog2(num_tile_inports + num_fu_inports + 1)),
      "FuInType": mk_bits(clog2(num_fu_inports + 1)),
      "FuOutType": mk_bits(clog2(num_fu_outports + 1)),
  }


def make_kernel_pkts(cfg, program_path):
  factory = ScriptFactory(
      path=program_path,
      CtrlType=cfg["CtrlType"],
      IntraCgraPktType=cfg["IntraCgraPktType"],
      CgraPayloadType=cfg["CgraPayloadType"],
      TileInType=cfg["TileInType"],
      FuOutType=cfg["FuOutType"],
      CMD_CONFIG_input=CMD_CONFIG,
      FuInType=cfg["FuInType"],
      ii=cfg["ii"],
      loop_times=0,  # unbounded: the kernel's RETURN ends execution
      CMD_CONST_input=CMD_CONST,
      CMD_CONFIG_COUNT_PER_ITER_input=CMD_CONFIG_COUNT_PER_ITER,
      CMD_CONFIG_TOTAL_CTRL_COUNT_input=CMD_CONFIG_TOTAL_CTRL_COUNT,
      CMD_CONFIG_PROLOGUE_FU_input=CMD_CONFIG_PROLOGUE_FU,
      CMD_CONFIG_PROLOGUE_ROUTING_CROSSBAR_input=CMD_CONFIG_PROLOGUE_ROUTING_CROSSBAR,
      CMD_CONFIG_PROLOGUE_FU_CROSSBAR_input=CMD_CONFIG_PROLOGUE_FU_CROSSBAR,
      CMD_LAUNCH_input=CMD_LAUNCH,
      DataType=cfg["DataType"],
      B1Type=b1,
      B2Type=b2,
      RegIdxType=cfg["RegIdxType"],
      CtrlAddrType=cfg["CtrlAddrType"],
      DataAddrType=cfg["DataAddrType"],
      num_registers_per_reg_bank=cfg["num_registers_per_reg_bank"])
  per_tile = factory.makeVectorCGRAPkts()
  pkts = []
  for key in sorted(per_tile, key=lambda xy: (xy[1], xy[0])):
    pkts.extend(per_tile[key])
  return pkts


def build_model_only(args):
  """Translates and Verilates the shared CgraRTL model into the cwd.

  Kernel runs started later from the same directory reuse it from pymtl3's
  import cache, so CI can build it once and hand it to every kernel job.
  """
  empty = {"array_config": {"columns": 4, "rows": 4, "compiled_ii": 1, "cores": []}}
  cfg = build_config(empty, 1, args)
  t0 = time.time()
  th = Harness(cfg, [], [], 1)
  th.elaborate()
  th.dut.set_metadata(VerilogVerilatorImportPass.vl_Wno_list,
                      ['UNSIGNED', 'UNOPTFLAT', 'WIDTH', 'WIDTHCONCAT', 'ALWCOMBORDER'])
  opts = {"test_verilog": "zeros", "test_yosys_verilog": False, "dump_textwave": False,
          "dump_vcd": False, "dump_vtb": ""}
  th = config_model_with_cmdline_opts(th, opts, duts=["dut"])
  finalize_verilator(th)
  print(f"vectorcgra: model built in {time.time() - t0:.0f}s", flush=True)
  return 0


def main():
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--program")
  ap.add_argument("--memory")
  ap.add_argument("--out")
  ap.add_argument("--build-model-only", action="store_true",
                  help="only translate + Verilate the shared CgraRTL model into the cwd")
  ap.add_argument("--backend", choices=["auto", "sim", "verilator"], default="auto",
                  help="auto = Verilator unless the program needs a simulation-only FU")
  ap.add_argument("--max-cycles", type=int, default=200000)
  ap.add_argument("--drain-cycles", type=int, default=0,
                  help="cycles to wait after RETURN before reading memory (0 = auto)")
  ap.add_argument("--registers-per-bank", type=int, default=8)
  ap.add_argument("--ctrl-mem-size", type=int, default=32,
                  help="ctrl memory entries (raised automatically if II needs more)")
  ap.add_argument("--data-mem-words", type=int, default=2048,
                  help="data memory words per CGRA (raised automatically if the image needs more)")
  ap.add_argument("--progress-every", type=int, default=5000)
  ap.add_argument("--translate-only", action="store_true",
                  help="only run the YAML -> VectorCGRA packet translation")
  ap.add_argument("--verbose-translator", action="store_true",
                  help="keep script_generator.py's per-instruction prints")
  args = ap.parse_args()
  if not args.build_model_only and not (args.program and args.memory and args.out):
    ap.error("--program, --memory and --out are required")

  if args.build_model_only:
    return build_model_only(args)

  program = yaml.safe_load(Path(args.program).read_text())
  layout = json.loads(Path(args.memory).read_text())
  memory_words = int(layout["memory_words"])
  init = [int(v) for v in layout["initial_memory"]]
  cfg = build_config(program, memory_words, args)

  backend = args.backend
  if backend == "auto":
    backend = "sim" if cfg["untranslatable"] else "verilator"
  if cfg["untranslatable"]:
    print("vectorcgra: program needs simulation-only FU(s): " + "; ".join(cfg["untranslatable"]) +
          (" -> using the pure-Python simulator" if backend == "sim" else ""), flush=True)
  args.backend = backend
  cfg["fu_list"] = make_fu_list(backend, cfg["opcodes"])

  max_time_step = 0
  for core in program["array_config"]["cores"]:
    for entry in core["entries"]:
      for inst in entry["instructions"]:
        for op in inst["operations"]:
          max_time_step = max(max_time_step, int(op.get("time_step", 0)))
  drain = args.drain_cycles or (4 * (max_time_step + cfg["ii"]) + 64)

  Pkt, Payload, DataType = cfg["IntraCgraPktType"], cfg["CgraPayloadType"], cfg["DataType"]
  preload = [Pkt(0, 0, payload=Payload(CMD_STORE_REQUEST, data=DataType(v & 0xFFFFFFFF, 1), data_addr=addr))
             for addr, v in enumerate(init)]

  t0 = time.time()
  # Translate YAML -> VectorCGRA packets with VectorCGRA's own translator.
  try:
    if args.verbose_translator:
      kernel_pkts = make_kernel_pkts(cfg, args.program)
    else:
      with open(os.devnull, "w") as devnull:
        saved = sys.stdout
        sys.stdout = devnull
        try:
          kernel_pkts = make_kernel_pkts(cfg, args.program)
        finally:
          sys.stdout = saved
  except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    msg = f"{type(e).__name__}: {e}".replace("\n", " ")
    if isinstance(e, KeyError):
      msg = f"opcode {e} is not supported by validation/script_generator.py"
    print(f"vectorcgra: TRANSLATION FAILED: {msg[:300]}", flush=True)
    return 4
  if args.translate_only:
    print(f"vectorcgra: translation OK ({len(kernel_pkts)} packets)", flush=True)
    return 0
  queries = [Pkt(payload=Payload(CMD_LOAD_REQUEST, data_addr=addr)) for addr in range(memory_words)]

  print(f"vectorcgra: {cfg['width']}x{cfg['height']} II={cfg['ii']} ctrl_mem={cfg['ctrl_mem_size']} "
        f"data_mem={cfg['data_mem_size_global']} (per-bank {cfg['data_mem_size_per_bank']}) "
        f"kernel_pkts={len(kernel_pkts)} preload={len(preload)} queries={len(queries)} "
        f"drain={drain} backend={args.backend}", flush=True)

  th = Harness(cfg, preload + kernel_pkts, queries, drain)
  th.elaborate()
  th.dut.set_metadata(VerilogVerilatorImportPass.vl_Wno_list,
                      ['UNSIGNED', 'UNOPTFLAT', 'WIDTH', 'WIDTHCONCAT', 'ALWCOMBORDER'])
  opts = {"test_verilog": "zeros" if args.backend == "verilator" else False,
          "test_yosys_verilog": False, "dump_textwave": False, "dump_vcd": False, "dump_vtb": ""}
  th = config_model_with_cmdline_opts(th, opts, duts=["dut"])
  th.apply(DefaultPassGroup(linetrace=False))
  th.sim_reset()
  t_setup = time.time() - t0
  print(f"vectorcgra: elaboration/setup took {t_setup:.1f}s", flush=True)

  complete_cycle = None
  timed_out = False
  t1 = time.time()
  try:
    while not th.done():
      if th.sim_cycle_count() >= args.max_cycles:
        timed_out = True
        break
      th.sim_tick()
      if complete_cycle is None and th.collector.complete_seen:
        complete_cycle = th.sim_cycle_count()
        print(f"vectorcgra: RETURN observed at cycle {complete_cycle}", flush=True)
      if args.progress_every and th.sim_cycle_count() % args.progress_every == 0:
        print(f"vectorcgra: cycle {th.sim_cycle_count()} ({time.time() - t1:.0f}s)", flush=True)
  finally:
    finalize_verilator(th)
  t_sim = time.time() - t1

  memory = list(init)
  returned = None
  read_back = 0
  for rec in th.collector.records:
    if rec["cmd"] == CMD_LOAD_RESPONSE:
      if 0 <= rec["addr"] < memory_words:
        memory[rec["addr"]] = to_int32(rec["data"])
        read_back += 1
    elif rec["cmd"] == CMD_COMPLETE and returned is None:
      returned = to_int32(rec["data"])

  result = {
      "simulator": "vectorcgra",
      "kernel": layout["kernel"],
      "return": returned if returned is not None else 0,
      "returned": returned is not None,
      "memory": memory,
      "memory_read_back": read_back == memory_words,
      "cycles": complete_cycle if complete_cycle is not None else th.sim_cycle_count(),
      "total_cycles": th.sim_cycle_count(),
      "timed_out": timed_out,
      "backend": args.backend,
      "wall_seconds": time.time() - t0,
      "setup_seconds": t_setup,
      "sim_seconds": t_sim,
  }
  Path(args.out).write_text(json.dumps(result))
  status = "TIMEOUT" if timed_out else ("done" if returned is not None else "no-return")
  print(f"vectorcgra: {status} cycles={result['cycles']} total={result['total_cycles']} "
        f"sim={t_sim:.1f}s ({th.sim_cycle_count() / max(t_sim, 1e-9):.0f} cyc/s)", flush=True)
  return 3 if timed_out else 0


if __name__ == "__main__":
  sys.exit(main())
