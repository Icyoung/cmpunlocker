/*
 * f0_corrupt_map: localize silent CE-memset corruption on 10GB CMP @ 80GiB.
 *
 * Observed: on the current driver state, cudaMemset([40G,60G), 0xAB)
 * returns success but leaves 18-78 KB of stale bytes near 40G+63K..404K
 * (f0_verify FAIL, 2/2 runs).  This tool maps the corruption precisely:
 *
 *   phase 1 (control): CE memset [0G,20G)  = 0xC1, SM-scan the same range
 *   phase 2:           CE memset [40G,60G) = 0xAB, SM-scan the same range
 *
 * Scan is done in 1 GiB chunks with sync + progress prints, so if the GPU
 * dies mid-scan we know the last live address.  Per-page (4 KiB) mismatch
 * counts are collected; host prints bad-page runs as offset ranges.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
#include <cuda_runtime.h>

#define GB        (1024ULL * 1024 * 1024)
#define PAGE      4096ULL
#define CHUNK     (1ULL * GB)

#define CHK(x) do { cudaError_t _e = (x); if (_e != cudaSuccess) { \
    printf("[cmap] FAIL %s: %s\n", #x, cudaGetErrorString(_e)); return 1; } } while (0)

static double ts_now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

/* count mismatched 32-bit words per 4KiB page; append bad page indices */
__global__ void scan_kernel(const uint32_t *base, size_t bytes, uint32_t expect,
                            uint32_t *bad_pages, uint32_t *bad_count, uint32_t max_list) {
    size_t n_words = bytes / 4;
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    size_t stride = (size_t)gridDim.x * blockDim.x;
    for (; i < n_words; i += stride) {
        if (base[i] != expect) {
            size_t page = (i * 4) / PAGE;
            uint32_t slot = atomicAdd(bad_count, 1);
            if (slot < max_list) bad_pages[slot] = (uint32_t)page;
            i = ((page + 1) * PAGE) / 4;  /* skip rest of page */
            i -= stride;                  /* compensate loop increment */
        }
    }
}

static int scan_range(const char *tag, uint32_t *dev, uint64_t off, uint64_t len, uint32_t expect) {
    uint32_t *bad_pages, *bad_count;
    const uint32_t max_list = 1u << 20;
    CHK(cudaMalloc((void**)&bad_pages, max_list * 4));
    CHK(cudaMalloc((void**)&bad_count, 4));

    uint64_t total_bad = 0;
    for (uint64_t c = 0; c < len; c += CHUNK) {
        double t0 = ts_now();
        CHK(cudaMemset(bad_count, 0, 4));
        size_t chunk = CHUNK;
        scan_kernel<<<1024, 256>>>((uint32_t*)((char*)dev + off + c), chunk, expect,
                                   bad_pages, bad_count, max_list);
        CHK(cudaDeviceSynchronize());
        uint32_t cnt = 0;
        CHK(cudaMemcpy(&cnt, bad_count, 4, cudaMemcpyDeviceToHost));
        total_bad += cnt;
        printf("[cmap] %s scan chunk %lluG..%lluG done badpages=%u (%.2fs)\n",
               tag, (unsigned long long)((off + c) / GB),
               (unsigned long long)((off + c + chunk) / GB), cnt, ts_now() - t0);
        fflush(stdout);
        if (cnt) {
            uint32_t take = cnt < max_list ? cnt : max_list;
            uint32_t *h = (uint32_t*)malloc(take * 4);
            CHK(cudaMemcpy(h, bad_pages, take * 4, cudaMemcpyDeviceToHost));
            /* print up to 40 runs */
            int runs = 0;
            uint32_t run_start = 0, prev = 0;
            int in_run = 0;
            for (uint32_t k = 0; k < take && runs < 40; k++) {
                if (!in_run) { run_start = prev = h[k]; in_run = 1; continue; }
                if (h[k] == prev + 1) { prev = h[k]; continue; }
                printf("[cmap]   %s bad run: +0x%llx..+0x%llx (%u pages)\n", tag,
                       (unsigned long long)(off + c + (uint64_t)run_start * PAGE),
                       (unsigned long long)(off + c + (uint64_t)(prev + 1) * PAGE - 1),
                       prev - run_start + 1);
                runs++; in_run = 0; k--;
            }
            if (in_run && runs < 40)
                printf("[cmap]   %s bad run: +0x%llx..+0x%llx (%u pages)\n", tag,
                       (unsigned long long)(off + c + (uint64_t)run_start * PAGE),
                       (unsigned long long)(off + c + (uint64_t)(prev + 1) * PAGE - 1),
                       prev - run_start + 1);
            free(h);
        }
    }
    printf("[cmap] %s TOTAL bad pages (words, approx): %llu\n", tag, (unsigned long long)total_bad);
    cudaFree(bad_pages); cudaFree(bad_count);
    return 0;
}

int main(void) {
    CHK(cudaSetDevice(0));
    CHK(cudaFree(0));
    char *dev = NULL;
    double t0 = ts_now();
    CHK(cudaMalloc((void**)&dev, 60ULL * GB));
    printf("[cmap] alloc 60G OK (%.2fs)\n", ts_now() - t0); fflush(stdout);

    /* control: low region */
    t0 = ts_now();
    CHK(cudaMemset(dev, 0xC1, 20ULL * GB));
    CHK(cudaDeviceSynchronize());
    printf("[cmap] CE memset [0,20G)=0xC1 done (%.3fs)\n", ts_now() - t0); fflush(stdout);
    if (scan_range("LOW ", (uint32_t*)dev, 0, 20ULL * GB, 0xC1C1C1C1u)) return 1;

    /* test: high region */
    t0 = ts_now();
    CHK(cudaMemset(dev + 40ULL * GB, 0xAB, 20ULL * GB));
    CHK(cudaDeviceSynchronize());
    printf("[cmap] CE memset [40G,60G)=0xAB done (%.3fs)\n", ts_now() - t0); fflush(stdout);
    if (scan_range("HIGH", (uint32_t*)dev, 40ULL * GB, 20ULL * GB, 0xABABABABu)) return 1;

    cudaFree(dev);
    printf("[cmap] DONE\n");
    return 0;
}
