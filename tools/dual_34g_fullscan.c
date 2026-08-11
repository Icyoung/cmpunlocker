/*
 * dual_34g_fullscan.c — child: alloc 34G, fill pat, wait for peer, full GPU scan.
 * Usage: dual_34g_fullscan <hexpat> <left|right>
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <cuda_runtime.h>

#define GB (1024ULL * 1024 * 1024)
#define SZ (34ULL * GB)

#define CHK(x) do { \
    cudaError_t _e = (x); \
    if (_e != cudaSuccess) { \
        fprintf(stderr, "FAIL %s: %s\n", #x, cudaGetErrorString(_e)); \
        return 2; \
    } \
} while (0)

__global__ void count_bad_u8(const unsigned char *p, size_t n, unsigned char pat,
                             unsigned long long *bad) {
    size_t stride = (size_t)gridDim.x * blockDim.x;
    for (size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x; i < n; i += stride)
        if (p[i] != pat)
            atomicAdd(bad, 1ULL);
}

static int fullscan(void *dev, unsigned char pat, const char *tag) {
    unsigned long long *d_bad = NULL;
    unsigned long long h_bad = 0;
    CHK(cudaMalloc((void **)&d_bad, sizeof(unsigned long long)));
    CHK(cudaMemset(d_bad, 0, sizeof(unsigned long long)));
    int blocks = 65535;
    int threads = 256;
    count_bad_u8<<<blocks, threads>>>((const unsigned char *)dev, SZ, pat, d_bad);
    CHK(cudaGetLastError());
    CHK(cudaDeviceSynchronize());
    CHK(cudaMemcpy(&h_bad, d_bad, sizeof(h_bad), cudaMemcpyDeviceToHost));
    CHK(cudaFree(d_bad));
    printf("%s FULLSCAN bad=%llu / %llu (%.6f%%) %s\n",
           tag, h_bad, (unsigned long long)SZ,
           100.0 * (double)h_bad / (double)SZ,
           h_bad ? "FAIL" : "OK");
    fflush(stdout);
    return h_bad ? 1 : 0;
}

static int band_samples(void *dev, unsigned char pat, const char *tag) {
    unsigned char host[4096];
    int fail = 0;
    static const size_t offs_g[] = {0, 1, 4, 8, 16, 32, 33, 34};
    for (size_t i = 0; i < sizeof(offs_g) / sizeof(offs_g[0]); i++) {
        size_t off = offs_g[i] * GB;
        if (off + sizeof(host) > SZ) continue;
        CHK(cudaMemcpy(host, (char *)dev + off, sizeof(host), cudaMemcpyDeviceToHost));
        size_t bad = 0;
        for (size_t j = 0; j < sizeof(host); j++)
            if (host[j] != pat) bad++;
        printf("%s band@%zuG 4KiB bad=%zu %s\n", tag, offs_g[i], bad, bad ? "FAIL" : "OK");
        if (bad) fail = 1;
    }
    fflush(stdout);
    return fail;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: %s <hexpat> <left|right>\n", argv[0]);
        return 1;
    }
    unsigned char pat = (unsigned char)strtol(argv[1], NULL, 16);
    const char *side = argv[2];
    const char *ready = "/tmp/dual34g_ready";
    char myflag[128];
    snprintf(myflag, sizeof(myflag), "/tmp/dual34g_ready_%s", side);

    void *dev = NULL;
    CHK(cudaSetDevice(0));
    CHK(cudaMalloc(&dev, SZ));
    CHK(cudaMemset(dev, pat, SZ));
    CHK(cudaDeviceSynchronize());

    printf("%s pid=%d ptr=%p pat=0x%02X size=34G post-fill scan:\n",
           side, getpid(), dev, pat);
    if (band_samples(dev, pat, side) || fullscan(dev, pat, side))
        return 3;

    /* signal ready, wait for peer */
    FILE *f = fopen(myflag, "w");
    if (f) { fprintf(f, "1\n"); fclose(f); }

    for (int i = 0; i < 120; i++) {
        if (access(ready, F_OK) == 0) break;
        if (access("/tmp/dual34g_ready_left", F_OK) == 0 &&
            access("/tmp/dual34g_ready_right", F_OK) == 0) {
            FILE *r = fopen(ready, "w");
            if (r) { fprintf(r, "go\n"); fclose(r); }
            break;
        }
        usleep(500000);
    }
  sleep(3);

    printf("%s pid=%d PEER-UP scan:\n", side, getpid());
    int fail = band_samples(dev, pat, side) | fullscan(dev, pat, side);
    CHK(cudaFree(dev));
    return fail ? 4 : 0;
}
