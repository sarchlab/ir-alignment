/* jacobi2d_int.c: one integer Jacobi-2D (5-point stencil) sweep for
 * ir-alignment.
 *
 * Zeonica's testbench only has a hand-mapped floating-point jacobi2d
 * assembly (Zeonica_Testbench/kernel/jacobi2d/jacobi2d.asm), so this C
 * version lets Neura generate the IR end to end. It updates the interior of
 * an 8x8 grid from the old grid A into B (B's border is left untouched).
 * The stencil uses the integer 5-point Laplacian (4*c - n - s - w - e) so no
 * division/right shift is needed.
 */
#define N 8

void kernel_jacobi2d_int(const int *A, int *B) {
  for (int i = 1; i < N - 1; ++i) {
    for (int j = 1; j < N - 1; ++j) {
      B[i * N + j] = 4 * A[i * N + j] - A[(i - 1) * N + j] - A[(i + 1) * N + j] -
                     A[i * N + j - 1] - A[i * N + j + 1];
    }
  }
}
