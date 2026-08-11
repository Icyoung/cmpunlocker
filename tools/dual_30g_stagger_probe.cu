/*
 * dual_30g_stagger_probe.cu — staggered dual 30G: band write/read only (no full memset).
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/wait.h>
#include <cuda_runtime.h>

#define GB (1024ULL * 1024 * 1024)
#define SZ (30ULL * 1024 * 1024 * 1024)
#define BAND (4096ULL)

#define CHK(x) do { \
    cudaError_t _e = (x); \
    if (_e != cudaSuccess) { \
        fprintf(stderr, "FAIL %s: %s\n", #x, cudaGetErrorString(_e)); \
        exit(2); \
    } \
} while (0)

static void logf(const char *s) {
    printf("%s\n", s);
    fflush(stdout);
}

static int probe_bands(void *dev, unsigned char pat, const char *tag) {
    size_t offs[] = {0, 1, 2, 4, 8, 12, 16, 20, 24, 28, 29};
    unsigned char host[BAND];
    int fail = 0;
    for (size_t i = 0; i < sizeof(offs) / sizeof(offs[0]); i++) {
        size_t off = offs[i] * GB;
        CHK(cudaMemset((char *)dev + off, pat, BAND));
        CHK(cudaDeviceSynchronize());
        CHK(cudaMemcpy(host, (char *)dev + off, BAND, cudaMemcpyDeviceToHost));
        size_t bad = 0;
        for (size_t j = 0; j < BAND; j++)
            if (host[j] != pat) bad++;
        printf("  %s @%zuG w/r bad=%zu %s\n", tag, offs[i], bad, bad ? "FAIL" : "OK");
        fflush(stdout);
        if (bad) fail = 1;
    }
    return fail;
}

static void child_left(void) {
    void *dev = NULL;
    CHK(cudaSetDevice(0));
    logf("[LEFT] cudaMalloc 30G...");
    CHK(cudaMalloc(&dev, SZ));
    printf("[LEFT] ptr=%p\n", dev); fflush(stdout);
    int bad = probe_bands(dev, 0xAA, "LEFT-solo");
    FILE *f = fopen("/tmp/dual30g_left_ready", "w");
    if (f) { fprintf(f, "%p\n", dev); fclose(f); }
    logf("[LEFT] ready, holding...");
    for (int i = 0; i < 120; i++) {
        if (access("/tmp/dual30g_right_done", F_OK) == 0) break;
        sleep(1);
    }
    logf("[LEFT] peer-up re-probe (must stay AA)");
    bad |= probe_bands(dev, 0xAA, "LEFT-peer");
    CHK(cudaFree(dev));
    exit(bad ? 1 : 0);
}

static void child_right(void) {
    for (int i = 0; i < 60; i++) {
        if (access("/tmp/dual30g_left_ready", F_OK) == 0) break;
        sleep(1);
    }
    sleep(1);
    void *dev = NULL;
    CHK(cudaSetDevice(0));
    logf("[RIGHT] cudaMalloc 30G (left up)...");
    CHK(cudaMalloc(&dev, SZ));
    printf("[RIGHT] ptr=%p\n", dev); fflush(stdout);
    int bad = probe_bands(dev, 0xBB, "RIGHT-solo");
    FILE *f = fopen("/tmp/dual30g_right_done", "w");
    if (f) { fprintf(f, "1\n"); fclose(f); }
    logf("[RIGHT] peer-up re-probe (must stay BB)");
    bad |= probe_bands(dev, 0xBB, "RIGHT-peer");
    CHK(cudaFree(dev));
    exit(bad ? 1 : 0);
}

int main(void) {
    setvbuf(stdout, NULL, _IONBF, 0);
    remove("/tmp/dual30g_left_ready");
    remove("/tmp/dual30g_right_done");
    logf("=== dual 30G stagger band-probe ===");
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
