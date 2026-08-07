/*
 * f0_alias_probe_ext: extended aliasing probe for the 10GB CMP @ 80GiB unlock.
 *
 * B8 (f0_alias_probe) proved there is no fold at exactly 40 GiB.  But the
 * crash boundary was never localized: memset(0,40G) is pathological while
 * small writes at 40G+ are fine.  This probe sweeps candidate fold
 * boundaries C and, for each, checks whether the page at (ANCHOR + C)
 * aliases the page at ANCHOR (1 GiB, definitely-safe region).
 *
 * For each C in {36,40,44,48,52,56,60,64,68,70,72,76} GiB:
 *   1. SM-write pattern A_C into 16 pages at ANCHOR
 *   2. SM-write pattern B_C into 16 pages at ANCHOR + C
 *   3. read back ANCHOR pages (must be A_C) and ANCHOR+C pages (must be B_C)
 *
 * If ANCHOR reads back B_C, then anchor+C aliases anchor -> fold at C.
 * If high pages don't hold B_C, writes at that address don't land at all.
 *
 * Only 32 pages touched per boundary; total touched < 3 MiB.  Designed to
 * stay far below whatever aggregate threshold kills the GSP.
 *
 * Env: ALLOC_GB (default 78), VERBOSE=1 for per-page detail.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
#include <cuda_runtime.h>

#define GB        (1024ULL * 1024 * 1024)
#define PAGE_U32  (4096ULL / 4)
#define N_PAGES   16
#define ANCHOR    (1ULL * GB)

#define CHK(x) do { cudaError_t _e = (x); if (_e != cudaSuccess) { \
    printf("FAIL %s: %s\n", #x, cudaGetErrorString(_e)); return 1; } } while (0)

static double ts_now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

__global__ void write_marker_kernel(uint32_t *base, size_t n_pages, uint32_t pat) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= n_pages) return;
    base[idx * PAGE_U32] = pat + idx;
}

__global__ void read_marker_kernel(const uint32_t *base, size_t n_pages, uint32_t *out) {
    int idx = blockIdx.x * blockDim.x + threadIdx.x;
    if (idx >= n_pages) return;
    out[idx] = base[idx * PAGE_U32];
}

int main(void) {
    static const uint64_t bounds[] = {36,40,44,48,52,56,60,64,68,70,72,76};
    const int nb = sizeof(bounds)/sizeof(bounds[0]);
    uint64_t alloc_gb = 78;
    if (getenv("ALLOC_GB")) alloc_gb = strtoull(getenv("ALLOC_GB"), 0, 0);

    CHK(cudaSetDevice(0));
    CHK(cudaFree(0));

    uint32_t *dev = NULL;
    double t0 = ts_now();
    CHK(cudaMalloc((void**)&dev, (size_t)alloc_gb * GB));
    printf("[alias_ext] alloc %llu GiB OK (%.2fs)\n",
           (unsigned long long)alloc_gb, ts_now() - t0);

    uint32_t *out_dev = NULL;
    CHK(cudaMalloc((void**)&out_dev, N_PAGES * sizeof(uint32_t)));
    uint32_t out[N_PAGES];

    int any_alias = 0, any_unlanded = 0;
    for (int b = 0; b < nb; b++) {
        uint64_t C = bounds[b] * GB;
        if (ANCHOR + C + N_PAGES * 4096ULL > alloc_gb * GB) {
            printf("[alias_ext] C=%lluG skipped (beyond alloc)\n",
                   (unsigned long long)bounds[b]);
            continue;
        }
        uint32_t patA = 0xA0000000u + (uint32_t)bounds[b];
        uint32_t patB = 0xB0000000u + (uint32_t)bounds[b];
        uint32_t *low  = (uint32_t *)((char *)dev + ANCHOR);
        uint32_t *high = (uint32_t *)((char *)dev + ANCHOR + C);

        t0 = ts_now();
        write_marker_kernel<<<1, N_PAGES>>>(low, N_PAGES, patA);
        CHK(cudaDeviceSynchronize());
        write_marker_kernel<<<1, N_PAGES>>>(high, N_PAGES, patB);
        CHK(cudaDeviceSynchronize());

        read_marker_kernel<<<1, N_PAGES>>>(low, N_PAGES, out_dev);
        CHK(cudaDeviceSynchronize());
        CHK(cudaMemcpy(out, out_dev, sizeof(out), cudaMemcpyDeviceToHost));
        int low_bad = 0;
        for (int i = 0; i < N_PAGES; i++)
            if (out[i] != patA + (uint32_t)i) low_bad++;

        read_marker_kernel<<<1, N_PAGES>>>(high, N_PAGES, out_dev);
        CHK(cudaDeviceSynchronize());
        CHK(cudaMemcpy(out, out_dev, sizeof(out), cudaMemcpyDeviceToHost));
        int high_bad = 0;
        for (int i = 0; i < N_PAGES; i++)
            if (out[i] != patB + (uint32_t)i) high_bad++;

        const char *verdict = "DISTINCT";
        if (low_bad)  { verdict = "ALIAS(low clobbered)"; any_alias = 1; }
        if (high_bad) { verdict = "UNLANDED(high write lost)"; any_unlanded = 1; }
        printf("[alias_ext] C=%2lluG  low_bad=%d high_bad=%d  %s  (%.3fs)\n",
               (unsigned long long)bounds[b], low_bad, high_bad, verdict, ts_now() - t0);
        fflush(stdout);
    }

    cudaFree(dev);
    printf("[alias_ext] DONE alias=%d unlanded=%d\n", any_alias, any_unlanded);
    return (any_alias || any_unlanded) ? 3 : 0;
}
