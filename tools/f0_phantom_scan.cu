/*
 * f0_phantom_scan: find live foreign structures inside a fresh user alloc.
 *
 * Theory under test (2026-08-07): the "phantom" that kills GSP is a
 * metadata structure (prime suspect: GPU MMU page-table pages) that the
 * allocator placed inside the user-data span of our own allocation.
 * Its position moves with heap layout (pinning 128 MiB at 40G moved the
 * death point from VA 39.7G to 37G).
 *
 * This tool is READ-ONLY and crash-safe: alloc ALLOC_GB, SM-scan every
 * 2 MiB block for nonzero content, print the nonzero-block map, and dump
 * the first few nonzero blocks to a file for offline analysis.
 *
 * Run as the FIRST big allocation after boot (before pattern-writing
 * warmups) so nonzero content = live structures, not stale test data.
 *
 * env: ALLOC_GB (default 60), DUMP_BLOCKS (default 8)
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
#include <cuda_runtime.h>

#define GB    (1024ULL * 1024 * 1024)
#define MB    (1024ULL * 1024)
#define BLOCK (2ULL * MB)

__global__ void block_nonzero_kernel(const uint32_t *base, size_t words_per_block,
                                     uint32_t *flags) {
    size_t blk = blockIdx.x;
    const uint32_t *p = base + blk * words_per_block;
    __shared__ uint32_t hit;
    if (threadIdx.x == 0) hit = 0;
    __syncthreads();
    size_t i = threadIdx.x;
    size_t stride = blockDim.x;
    for (; i < words_per_block; i += stride) {
        if (p[i] != 0) { hit = 1; break; }
    }
    __syncthreads();
    if (threadIdx.x == 0 && hit) flags[blk] = 1;
}

static double ts_now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int main(void) {
    uint64_t alloc_gb = getenv("ALLOC_GB") ? strtoull(getenv("ALLOC_GB"),0,0) : 60;
    int dump_blocks   = getenv("DUMP_BLOCKS") ? atoi(getenv("DUMP_BLOCKS")) : 8;

    cudaError_t e = cudaSetDevice(0);
    if (e) { printf("[scan] setdevice fail\n"); return 10; }
    cudaFree(0);

    char *dev = NULL;
    double t0 = ts_now();
    e = cudaMalloc((void**)&dev, (size_t)alloc_gb * GB);
    if (e) { printf("[scan] alloc fail: %s\n", cudaGetErrorString(e)); return 11; }
    printf("[scan] alloc %lluG OK (%.2fs)\n", (unsigned long long)alloc_gb, ts_now() - t0);

    size_t nblocks = (size_t)alloc_gb * GB / BLOCK;
    size_t wpb = BLOCK / 4;
    uint32_t *flags_dev = NULL;
    cudaMalloc((void**)&flags_dev, nblocks * 4);
    cudaMemset(flags_dev, 0, nblocks * 4);

    t0 = ts_now();
    block_nonzero_kernel<<<(unsigned)nblocks, 256>>>((const uint32_t*)dev, wpb, flags_dev);
    e = cudaDeviceSynchronize();
    if (e) { printf("[scan] scan kernel fail: %s\n", cudaGetErrorString(e)); return 12; }
    printf("[scan] full-range scan done (%.2fs)\n", ts_now() - t0);

    uint32_t *flags = (uint32_t*)malloc(nblocks * 4);
    cudaMemcpy(flags, flags_dev, nblocks * 4, cudaMemcpyDeviceToHost);

    /* report nonzero blocks as runs, recording run lengths */
    uint64_t total = 0;
    int in_run = 0; size_t run_start = 0;
    size_t small_runs[64]; int n_small = 0;   /* starts of runs <= 100 blocks */
    for (size_t b = 0; b < nblocks; b++) {
        if (flags[b]) {
            total++;
            if (!in_run) { in_run = 1; run_start = b; }
        } else if (in_run) {
            size_t runlen = b - run_start;
            printf("[scan] nonzero run: VA +0x%llx..+0x%llx (%zu blocks)\n",
                   (unsigned long long)(run_start * BLOCK),
                   (unsigned long long)(b * BLOCK - 1), runlen);
            if (runlen <= 100 && n_small < 64) small_runs[n_small++] = run_start;
            in_run = 0;
        }
    }
    if (in_run) {
        printf("[scan] nonzero run: VA +0x%llx..+0x%llx\n",
               (unsigned long long)(run_start * BLOCK),
               (unsigned long long)(nblocks * BLOCK - 1));
        if (nblocks - run_start <= 100 && n_small < 64) small_runs[n_small++] = run_start;
    }
    printf("[scan] total nonzero 2MiB blocks: %llu / %zu\n",
           (unsigned long long)total, nblocks);

    /* dump blocks from SMALL runs (isolated structures), not junk fields */
    FILE *f = fopen("/home/icy/f0/phantom_blocks.bin", "wb");
    if (f) {
        char *buf = (char*)malloc(BLOCK);
        int dumped = 0;
        for (int r = 0; r < n_small && dumped < dump_blocks; r++) {
            size_t b = small_runs[r];
            e = cudaMemcpy(buf, dev + b * BLOCK, BLOCK, cudaMemcpyDeviceToHost);
            if (e) break;
            fwrite(buf, 1, BLOCK, f);
            printf("[scan] dumped small-run block VA +0x%llx\n",
                   (unsigned long long)(b * BLOCK));
            dumped++;
        }
        fclose(f);
        free(buf);
    }

    cudaFree(dev);
    printf("[scan] DONE — GPU alive\n");
    return 0;
}
