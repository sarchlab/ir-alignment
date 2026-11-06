/* conv2d_int.c: integer 2D convolution (valid padding) for ir-alignment.
 *
 * Zeonica's testbench ships conv only as a hand-mapped systolic assembly
 * (Zeonica_Testbench/kernel/conv/conv2d_4x4_int.asm), so this C version lets
 * Neura generate the IR end to end. A 6x6 image is convolved with a 3x3
 * filter into a 4x4 output.
 */
#define IH 6
#define IW 6
#define KS 3
#define OH (IH - KS + 1)
#define OW (IW - KS + 1)

void kernel_conv2d_int(const int *in, const int *w, int *out) {
  for (int oi = 0; oi < OH; ++oi) {
    for (int oj = 0; oj < OW; ++oj) {
      int acc = 0;
      for (int ki = 0; ki < KS; ++ki) {
        for (int kj = 0; kj < KS; ++kj) {
          acc += in[(oi + ki) * IW + (oj + kj)] * w[ki * KS + kj];
        }
      }
      out[oi * OW + oj] = acc;
    }
  }
}
