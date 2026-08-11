/*
 * dual_30g_pair_fast.cu — staggered dual 30G, band-sample verify (no full 30G atomic scan).
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
#include <cuda_runtime.h>

#define GB (1024ULL * 1024 * 1024)
#define SZ (30ULL * GB)
#define BAND (4096ULL)

#define CHK(x) do { \
    cudaError_t _e = (x); \
    if (_e != cudaSuccess) { \
        fprintf(stderr, "FAIL %s: %s\n", #x, cudaGetErrorString(_e)); \
        exit(2); \
    } \
} while (0)

static int bands(void *dev, unsigned char pat, const char *tag) {
    unsigned char host[BAND];
    size_t offs[] = {0, 1, 2, 4, 8, 12, 16, 20, 24, 28, 29};
    int fail = 0;
    for (size_t i = 0; i < sizeof(offs) / sizeof(offs[0]); i++) {
        size_t off = offs[i] * GB;
        CHK(cudaMemcpy(host, (char *)dev + off, BAND, cudaMemcpyDeviceToHost));
        size_t bad = 0;
        for (size_t j = 0; j < BAND; j++)
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
    printf("[LEFT] ptr=%p alloc+fill AA\n", dev);
    int bad = bands(dev, 0xAA, "LEFT-solo");
    FILE *f = fopen("/tmp/dual30g_left_ready", "w");
    if (f) { fprintf(f, "%p\n", dev); fclose(f); }
    fflush(stdout);
    for (int i = 0; i < 180; i++) {
        if (access("/tmp/dual30g_right_done", F_OK) == 0) break;
        sleep(1);
    }
    printf("[LEFT] peer-up re-read (right should be BB, left must stay AA)\n");
    bad |= bands(dev, 0xAA, "LEFT-peer");
    CHK(cudaMemset(dev, 0xAA, 4 * 1024 * 1024));
    CHK(cudaDeviceSynchronize());
    bad |= bands(dev, 0xAA, "LEFT-touch");
    CHK(cudaFree(dev));
    exit(bad ? 1 : 0);
}

static void child_right(void) {
    for (int i = 0; i < 120; i++) {
        if (access("/tmp/dual30g_left_ready", F_OK) == 0) break;
        sleep(1);
    }
    sleep(1);
    void *dev = NULL;
    CHK(cudaSetDevice(0));
    CHK(cudaMalloc(&dev, SZ));
    CHK(cudaMemset(dev, 0xBB, SZ));
    CHK(cudaDeviceSynchronize());
    printf("[RIGHT] ptr=%p alloc+fill BB (left already up)\n", dev);
    int bad = bands(dev, 0xBB, "RIGHT-solo");
    FILE *f = fopen("/tmp/dual30g_right_done", "w");
    if (f) { fprintf(f, "1\n"); fclose(f); }
    fflush(stdout);
    sleep(2);
    printf("[RIGHT] peer-up re-read\n");
    bad |= bands(dev, 0xBB, "RIGHT-peer");
    CHK(cudaMemset(dev, 0xBB, 4 * 1024 * 1024));
    CHK(cudaDeviceSynchronize());
    bad |= bands(dev, 0xBB, "RIGHT-touch");
    CHK(cudaFree(dev));
    exit(bad ? 1 : 0);
}

int main(void) {
    remove("/tmp/dual30g_left_ready");
    remove("/tmp/dual30g_right_done");
    printf("=== dual 30G+30G staggered FAST (band verify) ===\n");
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
