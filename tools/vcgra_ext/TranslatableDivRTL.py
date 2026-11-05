"""
TranslatableDivRTL: VectorCGRA's DivRTL with a Verilog-translatable datapath.

The val/rdy handshake, predicate handling and supported operations
(OPT_DIV, OPT_DIV_CONST) are copied line for line from
third_party/VectorCGRA/fu/single/DivRTL.py; only the arithmetic
`a // b` is replaced by the combinational UDivComb divider (unsigned, the
same as pymtl3 Bits floor division). This lets kernels that divide run on
the Verilator backend instead of the pure-Python simulator, which is
thousands of times slower.

tools/vcgra_ext/test_udiv.py checks the divider bit-exactly against Python;
tools/vcgra_ext/check_divrtl_sync.py fails if upstream DivRTL's control
logic changes, so this copy cannot silently drift from it.
"""

from pymtl3 import *

from VectorCGRA.fu.basic.Fu import Fu
from VectorCGRA.lib.opt_type import *
from VectorCGRA.lib.util.data_struct_attr import kAttrPayload

from .UDivComb import UDivComb


class TranslatableDivRTL(Fu):

  def construct(s, CtrlPktType, num_inports, num_outports, vector_factor_power = 0):

    super(TranslatableDivRTL, s).construct(CtrlPktType, num_inports, num_outports, 1, vector_factor_power)

    num_entries = 2
    FuInType = mk_bits(clog2(num_inports + 1))
    CountType = mk_bits(clog2(num_entries + 1))
    PayloadType = s.DataType.get_field_type(kAttrPayload)

    s.in0 = Wire(FuInType)
    s.in1 = Wire(FuInType)

    idx_nbits = clog2(num_inports)
    s.in0_idx = Wire(idx_nbits)
    s.in1_idx = Wire(idx_nbits)

    s.in0_idx //= s.in0[0:idx_nbits]
    s.in1_idx //= s.in1[0:idx_nbits]

    s.recv_all_val = Wire(1)

    # Translatable replacement for `dividend // divisor`.
    s.udiv = UDivComb(PayloadType.nbits)
    s.div_dividend = Wire(PayloadType)
    s.div_divisor = Wire(PayloadType)
    s.udiv.dividend //= s.div_dividend
    s.udiv.divisor //= s.div_divisor

    @update
    def select_div_operands():
      s.div_dividend @= s.recv_in[s.in0_idx].msg.payload
      s.div_divisor @= s.recv_in[s.in1_idx].msg.payload
      if s.recv_opt.msg.operation == OPT_DIV_CONST:
        s.div_divisor @= s.recv_const.msg.payload

    @update
    def comb_logic():

      s.recv_all_val @= 0
      # For pick input register
      s.in0 @= 0
      s.in1 @= 0
      for i in range(num_inports):
        s.recv_in[i].rdy @= b1(0)
      for i in range(num_outports):
        s.send_out[i].val @= 0
        s.send_out[i].msg @= s.DataType()

      s.recv_const.rdy @= 0
      s.recv_opt.rdy @= 0

      s.send_to_ctrl_mem.val @= 0
      s.send_to_ctrl_mem.msg @= s.CgraPayloadType(0, 0, 0, 0, 0)
      s.recv_from_ctrl_mem.rdy @= 0

      if s.recv_opt.val:
        if s.recv_opt.msg.fu_in[0] != 0:
          s.in0 @= zext(s.recv_opt.msg.fu_in[0] - 1, FuInType)
        if s.recv_opt.msg.fu_in[1] != 0:
          s.in1 @= zext(s.recv_opt.msg.fu_in[1] - 1, FuInType)

      if s.recv_opt.val:
        if s.recv_opt.msg.operation == OPT_DIV:
          s.send_out[0].msg.payload @= s.udiv.quotient
          s.send_out[0].msg.predicate @= s.recv_in[s.in0_idx].msg.predicate & \
                                         s.recv_in[s.in1_idx].msg.predicate & \
                                         s.reached_vector_factor
          s.recv_all_val @= s.recv_in[s.in0_idx].val & s.recv_in[s.in1_idx].val
          s.send_out[0].val @= s.recv_all_val
          s.recv_in[s.in0_idx].rdy @= s.recv_all_val & s.send_out[0].rdy
          s.recv_in[s.in1_idx].rdy @= s.recv_all_val & s.send_out[0].rdy
          s.recv_opt.rdy @= s.recv_all_val & s.send_out[0].rdy

        elif s.recv_opt.msg.operation == OPT_DIV_CONST:
          s.send_out[0].msg.payload @= s.udiv.quotient
          s.send_out[0].msg.predicate @= s.recv_in[s.in0_idx].msg.predicate & \
                                         s.reached_vector_factor
          s.recv_all_val @= s.recv_in[s.in0_idx].val & s.recv_const.val
          s.send_out[0].val @= s.recv_all_val
          s.recv_in[s.in0_idx].rdy @= s.recv_all_val & s.send_out[0].rdy
          s.recv_const.rdy @= s.recv_all_val & s.send_out[0].rdy
          s.recv_opt.rdy @= s.recv_all_val & s.send_out[0].rdy

        else:
          for j in range(num_outports):
            s.send_out[j].val @= b1(0)
          s.recv_opt.rdy @= 0
          s.recv_in[s.in0_idx].rdy @= 0
          s.recv_in[s.in1_idx].rdy @= 0
