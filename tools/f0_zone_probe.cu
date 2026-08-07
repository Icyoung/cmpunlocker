/*
 * f0_zone_probe: which physical zone kills the GSP — size or address?
 *
 * After the 2026-08-07 ladder crash (62G write in 78G alloc -> GSP store
 * fault in ~1s), the open question is whether the poison is the write
 * SIZE or the physical ADDRESSES touched.  This tool runs fork-isolated
 * zone writes inside one big allocation, safest first, with per-1GiB
 * chunk progress + bandwidth prints so a hang tells us exactly where.
 *
 * Zones (env-overridable):
 *   A: [ 0G,20G)  in ALLOC_GB   (control, known-safe pattern)
 *   B: [40G,60G)  in ALLOC_GB   (mid-high; known-good inside 60G allocs)
 *   C: [58G,78G)  in ALLOC_GB   (the suspected poison zone)
 *
 * usage: f0_zone_probe [zones]   e.g. f0_zone_probe ABC (default), f0_zone_probe C
 * env:   ALLOC_GB (default 78), CHILD_TIMEOUT_SEC (default 60)
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <time.h>
#include <unistd.h>
#include <sys/wait.h>
#include <cuda_runtime.h>

#define GB (1024ULL * 1024 * 1024)

__global__ void fill_kernel(uint32_t *p, size_t n_words, uint32_t pat) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    size_t stride = (size_t)gridDim.x * blockDim.x;
    for (; i < n_words; i += stride) p[i] = pat;
}

static double ts_now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static int child_run(uint64_t alloc_gb, uint64_t off_gb, uint64_t len_gb, char tag) {
    if (cudaSetDevice(0) != cudaSuccess) return 10;
    if (cudaFree(0) != cudaSuccess) return 11;
    uint32_t *dev = NULL;
    double t0 = ts_now();
    if (cudaMalloc((void**)&dev, (size_t)alloc_gb * GB) != cudaSuccess) return 12;
    printf("[zone %c] alloc %lluG OK (%.2fs), writing [%lluG,%lluG)\n", tag,
           (unsigned long long)alloc_gb, ts_now() - t0,
           (unsigned long long)off_gb, (unsigned long long)(off_gb + len_gb));
    fflush(stdout);

    for (uint64_t c = 0; c < len_gb; c++) {
        uint32_t *p = (uint32_t*)((char*)dev + (off_gb + c) * GB);
        t0 = ts_now();
        fill_kernel<<<2048, 256>>>(p, (size_t)GB / 4, 0x5A5A5A5Au);
        cudaError_t e = cudaDeviceSynchronize();
        double dt = ts_now() - t0;
        printf("[zone %c] chunk %lluG..%lluG %s %.3fs (%.0f GB/s)\n", tag,
               (unsigned long long)(off_gb + c), (unsigned long long)(off_gb + c + 1),
               e == cudaSuccess ? "OK" : cudaGetErrorString(e), dt,
               dt > 0 ? 1.0 / dt : 0.0);
        fflush(stdout);
        if (e != cudaSuccess) return 13;
    }
    printf("[zone %c] WRITE COMPLETE\n", tag);
    fflush(stdout);
    cudaFree(dev);
    printf("[zone %c] FREE OK\n", tag);
    fflush(stdout);
    return 0;
}

int main(int argc, char **argv) {
    const char *zones = argc > 1 ? argv[1] : "ABC";
    uint64_t alloc_gb = getenv("ALLOC_GB") ? strtoull(getenv("ALLOC_GB"), 0, 0) : 78;
    int timeout = getenv("CHILD_TIMEOUT_SEC") ? atoi(getenv("CHILD_TIMEOUT_SEC")) : 60;

    struct { char tag; uint64_t off, len; } all[] = {
        {'A', 0, 20}, {'B', 40, 20}, {'C', 58, 20},
    };
    for (const char *z = zones; *z; z++) {
        int found = -1;
        for (unsigned k = 0; k < sizeof(all)/sizeof(all[0]); k++)
            if (all[k].tag == *z) found = k;
        if (found < 0) { printf("[zone] unknown zone %c\n", *z); continue; }
        uint64_t off = all[found].off, len = all[found].len;
        if (off + len > alloc_gb) { printf("[zone %c] skipped (beyond alloc)\n", *z); continue; }
        printf("[zone] === fork zone %c: [%lluG,%lluG) in %lluG alloc ===\n",
               *z, (unsigned long long)off, (unsigned long long)(off + len),
               (unsigned long long)alloc_gb);
        fflush(stdout);
        pid_t pid = fork();
        if (pid == 0) _exit(child_run(alloc_gb, off, len, *z));
        double t0 = ts_now();
        int status = 0; pid_t r = 0;
        while (ts_now() - t0 < timeout) {
            r = waitpid(pid, &status, WNOHANG);
            if (r == pid) break;
            usleep(100000);
        }
        if (r != pid) {
            printf("[zone] CHILD HUNG in zone %c (>%ds) — GPU likely dying; STOPPING\n", *z, timeout);
            return 2;
        }
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
            printf("[zone] child abnormal in zone %c (status=0x%x); STOPPING\n", *z, status);
            return 3;
        }
        printf("[zone] zone %c done\n", *z);
        fflush(stdout);
    }
    printf("[zone] ALL ZONES PASS\n");
    return 0;
}
