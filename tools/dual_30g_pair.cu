/*
 * dual_30g_pair.cu — staggered dual 30G: left solo verify -> hold -> right up -> both verify.
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
#include <cuda_runtime.h>

#define GB (1024ULL * 1024 * 1024)
#define SZ (30ULL * GB)

#define CHK(x) do { \
    cudaError_t _e = (x); \
    if (_e != cudaSuccess) { \
        fprintf(stderr, "FAIL %s: %s\n", #x, cudaGetErrorString(_e)); \
        exit(2); \
    } \
} while (0)

__global__ void count_bad_u8(const unsigned char *p, size_t n, unsigned char pat,
                             unsigned long long *bad) {
    size_t stride = (size_t)gridDim.x * blockDim.x;
    for (size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x; i < n; i += stride)
        if (p[i] != pat)
            atomicAdd(bad, 1ULL);
}

static unsigned long long fullscan(void *dev, unsigned char pat) {
    unsigned long long *d_bad = NULL, h_bad = 0;
    CHK(cudaMalloc((void **)&d_bad, sizeof(unsigned long long)));
    CHK(cudaMemset(d_bad, 0, sizeof(unsigned long long)));
    count_bad_u8<<<65535, 256>>>((const unsigned char *)dev, SZ, pat, d_bad);
    CHK(cudaDeviceSynchronize());
    CHK(cudaMemcpy(&h_bad, d_bad, sizeof(h_bad), cudaMemcpyDeviceToHost));
    CHK(cudaFree(d_bad));
    return h_bad;
}

static int bands_ok(void *dev, unsigned char pat, const char *tag) {
    unsigned char host[4096];
    size_t offs[] = {0, 1, 4, 8, 16, 24, 29};
    int fail = 0;
    for (size_t i = 0; i < sizeof(offs) / sizeof(offs[0]); i++) {
        size_t off = offs[i] * GB;
        CHK(cudaMemcpy(host, (char *)dev + off, sizeof(host), cudaMemcpyDeviceToHost));
        size_t bad = 0;
        for (size_t j = 0; j < sizeof(host); j++)
            if (host[j] != pat) bad++;
        printf("  %s @%zuG 4KiB bad=%zu %s\n", tag, offs[i], bad, bad ? "FAIL" : "OK");
        if (bad) fail = 1;
    }
    return fail;
}

static void child_left(void) {
    void *dev = NULL;
    CHK(cudaSetDevice(0));
    CHK(cudaMalloc(&dev, SZ));
    CHK(cudaMemset(dev, 0xAA, SZ));
    CHK(cudaDeviceSynchronize());
    printf("[LEFT] ptr=%p solo-verify\n", dev);
    unsigned long long bad = fullscan(dev, 0xAA);
    printf("[LEFT] SOLO FULLSCAN bad=%llu %s\n", bad, bad ? "FAIL" : "OK");
    if (bands_ok(dev, 0xAA, "LEFT-solo")) bad = 1;
    FILE *f = fopen("/tmp/dual30g_left_ready", "w");
    if (f) { fprintf(f, "%p\n", dev); fclose(f); }
    fflush(stdout);
    /* hold until right done */
    for (int i = 0; i < 300; i++) {
        if (access("/tmp/dual30g_right_done", F_OK) == 0) break;
        sleep(1);
    }
    printf("[LEFT] PEER-UP re-verify\n");
    unsigned long long bad2 = fullscan(dev, 0xAA);
    printf("[LEFT] PEER FULLSCAN bad=%llu %s\n", bad2, bad2 ? "FAIL" : "OK");
    bands_ok(dev, 0xAA, "LEFT-peer");
    CHK(cudaMemset(dev, 0xAA, 4 * 1024 * 1024));
    CHK(cudaDeviceSynchronize());
    bad2 = fullscan(dev, 0xAA);
    printf("[LEFT] AFTER-TOUCH bad=%llu %s\n", bad2, bad2 ? "FAIL" : "OK");
    CHK(cudaFree(dev));
    exit(bad || bad2 ? 1 : 0);
}

static void child_right(void) {
    for (int i = 0; i < 120; i++) {
        if (access("/tmp/dual30g_left_ready", F_OK) == 0) break;
        sleep(1);
    }
    sleep(2);
    void *dev = NULL;
    CHK(cudaSetDevice(0));
    CHK(cudaMalloc(&dev, SZ));
    CHK(cudaMemset(dev, 0xBB, SZ));
    CHK(cudaDeviceSynchronize());
    printf("[RIGHT] ptr=%p solo-verify (left already up)\n", dev);
    unsigned long long bad = fullscan(dev, 0xBB);
    printf("[RIGHT] SOLO FULLSCAN bad=%llu %s\n", bad, bad ? "FAIL" : "OK");
    bands_ok(dev, 0xBB, "RIGHT-solo");
    FILE *f = fopen("/tmp/dual30g_right_done", "w");
    if (f) { fprintf(f, "1\n"); fclose(f); }
    sleep(3);
    printf("[RIGHT] PEER-UP re-verify\n");
    unsigned long long bad2 = fullscan(dev, 0xBB);
    printf("[RIGHT] PEER FULLSCAN bad=%llu %s\n", bad2, bad2 ? "FAIL" : "OK");
    bands_ok(dev, 0xBB, "RIGHT-peer");
    CHK(cudaMemset(dev, 0xBB, 4 * 1024 * 1024));
    CHK(cudaDeviceSynchronize());
    bad2 = fullscan(dev, 0xBB);
    printf("[RIGHT] AFTER-TOUCH bad=%llu %s\n", bad2, bad2 ? "FAIL" : "OK");
    CHK(cudaFree(dev));
    exit(bad || bad2 ? 1 : 0);
}

int main(void) {
    remove("/tmp/dual30g_left_ready");
    remove("/tmp/dual30g_right_done");
    printf("=== dual 30G+30G staggered (left first, then right) ===\n");
    pid_t L = fork();
    if (L == 0) child_left();
    pid_t R = fork();
    if (R == 0) child_right();
    int stL = 0, stR = 0;
    waitpid(L, &stL, 0);
    waitpid(R, &stR, 0);
    printf("=== exit left=%d right=%d ===\n",
           WIFEXITED(stL) ? WEXITSTATUS(stL) : -1,
           WIFEXITED(stR) ? WEXITSTATUS(stR) : -1);
    return (WIFEXITED(stL) && WEXITSTATUS(stL)) || (WIFEXITED(stR) && WEXITSTATUS(stR));
}
