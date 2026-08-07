/*
 * f0_phantom_peek: read the phantom structure through the double-allocation.
 *
 * The drip proved writing VA [39G+704M, 39G+768M) of a 60G alloc kills GSP
 * (the write clobbers a structure GSP owns).  For the write to clobber it,
 * our VA must map the phantom's PA — so we can also just READ it.
 *
 * This tool allocs 60G and D2H-copies the phantom window to a host file,
 * WITHOUT writing anything to it.  Content analysis then identifies the
 * owner (PTE arrays / GSP structs / RPC buffers all look different).
 *
 * Must run before any large writes in the boot (phantom must be intact).
 *
 * env: PEEK_OFF_GB (default 39), PEEK_OFF_MB (default 704),
 *      PEEK_LEN_MB (default 16), ALLOC_GB (default 60)
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
#include <cuda_runtime.h>

#define GB (1024ULL * 1024 * 1024)
#define MB (1024ULL * 1024)

static double ts_now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int main(void) {
    uint64_t alloc_gb   = getenv("ALLOC_GB")    ? strtoull(getenv("ALLOC_GB"),0,0)    : 60;
    uint64_t off_gb     = getenv("PEEK_OFF_GB") ? strtoull(getenv("PEEK_OFF_GB"),0,0) : 39;
    uint64_t off_mb     = getenv("PEEK_OFF_MB") ? strtoull(getenv("PEEK_OFF_MB"),0,0) : 704;
    uint64_t len_mb     = getenv("PEEK_LEN_MB") ? strtoull(getenv("PEEK_LEN_MB"),0,0) : 16;

    cudaError_t e = cudaSetDevice(0);
    if (e) { printf("[peek] setdevice fail\n"); return 10; }
    cudaFree(0);

    char *dev = NULL;
    double t0 = ts_now();
    e = cudaMalloc((void**)&dev, (size_t)alloc_gb * GB);
    if (e) { printf("[peek] alloc fail: %s\n", cudaGetErrorString(e)); return 11; }
    printf("[peek] alloc %lluG OK (%.2fs)\n", (unsigned long long)alloc_gb, ts_now() - t0);

    uint64_t off = off_gb * GB + off_mb * MB;
    uint64_t len = len_mb * MB;
    FILE *f = fopen("/home/icy/f0/phantom.bin", "wb");
    if (!f) { printf("[peek] cannot open output\n"); return 12; }

    const uint64_t step = 1 * MB;
    char *buf = (char*)malloc(step);
    for (uint64_t p = 0; p < len; p += step) {
        e = cudaMemcpy(buf, dev + off + p, step, cudaMemcpyDeviceToHost);
        if (e) {
            printf("[peek] D2H fail at +%lluMB: %s\n",
                   (unsigned long long)(p / MB), cudaGetErrorString(e));
            fclose(f); return 13;
        }
        fwrite(buf, 1, step, f);
    }
    fclose(f);
    printf("[peek] dumped %llu MB from VA +%lluG+%lluMB -> /home/icy/f0/phantom.bin (%.1fs)\n",
           (unsigned long long)len_mb, (unsigned long long)off_gb,
           (unsigned long long)off_mb, ts_now() - t0);

    /* control window */
    f = fopen("/home/icy/f0/phantom_ctrl.bin", "wb");
    if (f) {
        for (uint64_t p = 0; p < 4 * MB; p += step) {
            e = cudaMemcpy(buf, dev + GB + p, step, cudaMemcpyDeviceToHost);
            if (e) break;
            fwrite(buf, 1, step, f);
        }
        fclose(f);
        printf("[peek] control window dumped\n");
    }

    cudaFree(dev);
    printf("[peek] DONE — GPU still alive\n");
    return 0;
}
