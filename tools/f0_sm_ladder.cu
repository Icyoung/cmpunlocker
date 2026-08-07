/*
 * f0_sm_ladder: fork-isolated SM bulk-write ladder on 10GB CMP @ 80GiB.
 *
 * Known safe:   60 GiB SM full write (f0_ce_d2h), all page-probe writes.
 * Known fatal:  70 GiB SM prefill (96s pathology -> GSP illegal instruction).
 * This tool finds the exact crossover: for each N in the ladder, fork a
 * child that cudaMallocs ALLOC_GB, SM-writes [0, N), syncs, prints the
 * duration, exits.  Parent watches with a timeout; a hung child means the
 * GPU is dying — stop the ladder (parent exits without further forks).
 *
 * usage: f0_sm_ladder [startGB] [endGB] [stepGB]   (default 62 70 2)
 * env:   ALLOC_GB (default 78), CHILD_TIMEOUT_SEC (default 120)
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

static int child_run(uint64_t alloc_gb, uint64_t write_gb) {
    if (cudaSetDevice(0) != cudaSuccess) return 10;
    if (cudaFree(0) != cudaSuccess) return 11;
    uint32_t *dev = NULL;
    double t0 = ts_now();
    if (cudaMalloc((void**)&dev, (size_t)alloc_gb * GB) != cudaSuccess) return 12;
    double t_alloc = ts_now() - t0;

    size_t n_words = (size_t)write_gb * GB / 4;
    t0 = ts_now();
    fill_kernel<<<2048, 256>>>(dev, n_words, 0x5A5A5A5Au);
    cudaError_t e = cudaDeviceSynchronize();
    double t_write = ts_now() - t0;

    /* note: child exit code carries only the low byte; print is the record */
    printf("[ladder-child] write %lluG in %lluG alloc: %s  alloc=%.2fs write=%.3fs (%.1f GB/s)\n",
           (unsigned long long)write_gb, (unsigned long long)alloc_gb,
           e == cudaSuccess ? "OK" : cudaGetErrorString(e),
           t_alloc, t_write, t_write > 0 ? write_gb / t_write : 0.0);
    fflush(stdout);
    cudaFree(dev);
    return e == cudaSuccess ? 0 : 13;
}

int main(int argc, char **argv) {
    uint64_t start = 62, end = 70, step = 2;
    if (argc > 1) start = strtoull(argv[1], 0, 0);
    if (argc > 2) end   = strtoull(argv[2], 0, 0);
    if (argc > 3) step  = strtoull(argv[3], 0, 0);
    uint64_t alloc_gb = getenv("ALLOC_GB") ? strtoull(getenv("ALLOC_GB"), 0, 0) : 78;
    int timeout = getenv("CHILD_TIMEOUT_SEC") ? atoi(getenv("CHILD_TIMEOUT_SEC")) : 120;

    printf("[ladder] SM write ladder %lluG..%lluG step %lluG, alloc=%lluG, timeout=%ds\n",
           (unsigned long long)start, (unsigned long long)end,
           (unsigned long long)step, (unsigned long long)alloc_gb, timeout);
    fflush(stdout);

    for (uint64_t n = start; n <= end; n += step) {
        printf("[ladder] --- fork for %lluG ---\n", (unsigned long long)n);
        fflush(stdout);
        pid_t pid = fork();
        if (pid == 0) _exit(child_run(alloc_gb, n));

        double t0 = ts_now();
        int status = 0;
        pid_t r = 0;
        while (ts_now() - t0 < timeout) {
            r = waitpid(pid, &status, WNOHANG);
            if (r == pid) break;
            usleep(200000);
        }
        if (r != pid) {
            printf("[ladder] CHILD HUNG at %lluG (>%ds) — GPU likely dying; STOPPING LADDER\n",
                   (unsigned long long)n, timeout);
            fflush(stdout);
            /* do not kill: a hung CUDA context is unkillable anyway; leave for forensics */
            return 2;
        }
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
            printf("[ladder] child exit abnormal at %lluG (status=0x%x); STOPPING\n",
                   (unsigned long long)n, status);
            fflush(stdout);
            return 3;
        }
    }
    printf("[ladder] ALL SIZES PASS\n");
    return 0;
}
