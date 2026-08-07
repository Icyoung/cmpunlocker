/*
 * f0_slow_drip: localize the phantom GSP structure by 1 GiB steps.
 *
 * 2026-08-07 finding: after a 60G single-launch SM write, GSP dereferences
 * a pointer slot that physically sits inside the user allocation (crash
 * mbadaddr == our fill pattern).  Instead of crashing blind, this tool
 * writes the alloc in 1 GiB chunks and after each chunk performs a tiny
 * alloc/free RPC roundtrip (forces a GSP_RM_ALLOC so a dead GSP is
 * detected in-process).  The chunk index at death = the phantom's
 * location to 1 GiB; rerun with CHUNK_MB for finer grain.
 *
 * usage: f0_slow_drip [startGB] [endGB]
 * env:   ALLOC_GB (default 60), CHUNK_MB (default 1024)
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
#include <unistd.h>
#include <cuda_runtime.h>

#define GB (1024ULL * 1024 * 1024)
#define MB (1024ULL * 1024)

__global__ void fill_kernel(uint32_t *p, size_t n_words, uint32_t pat) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    size_t stride = (size_t)gridDim.x * blockDim.x;
    for (; i < n_words; i += stride) p[i] = pat;
}

static double ts_now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv) {
    uint64_t alloc_gb = getenv("ALLOC_GB") ? strtoull(getenv("ALLOC_GB"), 0, 0) : 60;
    uint64_t chunk_mb = getenv("CHUNK_MB") ? strtoull(getenv("CHUNK_MB"), 0, 0) : 1024;
    uint64_t start_gb = argc > 1 ? strtoull(argv[1], 0, 0) : 0;
    uint64_t end_gb   = argc > 2 ? strtoull(argv[2], 0, 0) : alloc_gb;

    cudaError_t e = cudaSetDevice(0);
    if (e) { printf("[drip] setdevice fail\n"); return 10; }
    cudaFree(0);
    { FILE *pf = fopen("/home/icy/f0/drip_progress.log", "w"); if (pf) fclose(pf); }

    uint32_t *dev = NULL;
    double t0 = ts_now();
    e = cudaMalloc((void**)&dev, (size_t)alloc_gb * GB);
    if (e) { printf("[drip] alloc fail: %s\n", cudaGetErrorString(e)); return 11; }
    printf("[drip] alloc %lluG OK (%.2fs), dripping [%lluG,%lluG) in %lluMB chunks\n",
           (unsigned long long)alloc_gb, ts_now() - t0,
           (unsigned long long)start_gb, (unsigned long long)end_gb,
           (unsigned long long)chunk_mb);
    fflush(stdout);

    for (uint64_t off = start_gb * GB; off < end_gb * GB; off += chunk_mb * MB) {
        uint64_t chunk = chunk_mb * MB;
        if (off + chunk > end_gb * GB) chunk = end_gb * GB - off;
        /* progress must survive a wedged process: log to file with fsync */
        FILE *pf = fopen("/home/icy/f0/drip_progress.log", "a");
        if (pf) {
            fprintf(pf, "WRITING +%lluG+%04lluMB\n",
                    (unsigned long long)(off / GB),
                    (unsigned long long)((off % GB) / MB));
            fflush(pf); fsync(fileno(pf)); fclose(pf);
        }
        t0 = ts_now();
        fill_kernel<<<1024, 256>>>((uint32_t*)((char*)dev + off), chunk / 4, 0x5A5A5A5Au);
        e = cudaDeviceSynchronize();
        if (e) {
            printf("[drip] WRITE FAIL at +%lluG (+%lluMB): %s\n",
                   (unsigned long long)(off / GB),
                   (unsigned long long)((off % GB) / MB), cudaGetErrorString(e));
            fflush(stdout);
            return 12;
        }
        /* RPC roundtrip: tiny alloc+free forces GSP_RM_ALLOC/ FREE */
        void *tmp = NULL;
        e = cudaMalloc(&tmp, 4 * MB);
        if (e) {
            printf("[drip] RPC-ALLOC FAIL after chunk +%lluG+%lluMB: %s  <== DEATH POINT\n",
                   (unsigned long long)(off / GB),
                   (unsigned long long)((off % GB) / MB), cudaGetErrorString(e));
            fflush(stdout);
            return 13;
        }
        e = cudaFree(tmp);
        if (e) {
            printf("[drip] RPC-FREE FAIL after chunk +%lluG+%lluMB: %s  <== DEATH POINT\n",
                   (unsigned long long)(off / GB),
                   (unsigned long long)((off % GB) / MB), cudaGetErrorString(e));
            fflush(stdout);
            return 14;
        }
        printf("[drip] chunk +%lluG+%04lluMB OK (%.3fs)\n",
               (unsigned long long)(off / GB),
               (unsigned long long)((off % GB) / MB), ts_now() - t0);
        fflush(stdout);
    }
    printf("[drip] ALL CHUNKS OK — phantom not hit in [%lluG,%lluG)\n",
           (unsigned long long)start_gb, (unsigned long long)end_gb);
    cudaFree(dev);
    printf("[drip] FINAL FREE OK\n");
    return 0;
}
