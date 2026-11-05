"""
UDivComb: combinational unsigned divider that pymtl3 can translate to Verilog.

VectorCGRA's DivRTL computes `a // b` with Python's floor-division operator,
which pymtl3's RTLIR translator rejects, so a CgraRTL containing DivRTL can
only run on the (very slow) pure-Python simulator. This component computes
the same unsigned quotient/remainder with a restoring-division array built
from add/compare/shift only, which translates and Verilates.

Division by zero yields quotient = all ones and remainder = dividend (the
usual hardware convention); DivRTL would raise ZeroDivisionError in Python.
"""

from pymtl3 import *


class UDivComb(Component):

  def construct(s, nbits):
    s.dividend = InPort(nbits)
    s.divisor = InPort(nbits)
    s.quotient = OutPort(nbits)
    s.remainder = OutPort(nbits)

    WideType = mk_bits(nbits + 1)
    # Partial remainders / quotients after each of the `nbits` steps.
    s.rem = [Wire(WideType) for _ in range(nbits + 1)]
    s.quo = [Wire(nbits) for _ in range(nbits + 1)]
    s.shifted = [Wire(WideType) for _ in range(nbits)]

    @update
    def restoring_division():
      s.rem[0] @= WideType(0)
      s.quo[0] @= 0
      for i in range(nbits):
        # Bring down the next dividend bit (MSB first).
        s.shifted[i] @= (s.rem[i] << 1) | zext(s.dividend[nbits - 1 - i], WideType)
        if s.shifted[i] >= zext(s.divisor, WideType):
          s.rem[i + 1] @= s.shifted[i] - zext(s.divisor, WideType)
          s.quo[i + 1] @= (s.quo[i] << 1) | 1
        else:
          s.rem[i + 1] @= s.shifted[i]
          s.quo[i + 1] @= s.quo[i] << 1

    s.quotient //= s.quo[nbits]
    s.remainder //= s.rem[nbits][0:nbits]
