/*
 * prove_80g.c — show ~80G physical VRAM exists independently of +32G PTE aliasing.
 *
 * Test A: one 55G mapping spans hole -> rewrite before hole poisons after (alias).
 * Test B: two separate ~34G allocs (each single-segment) -> distinct patterns, no crosstalk.
 * Test C: sum allocated + cudaMemGetInfo vs 40G ceiling.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <sys/wait.h>
#include <cuda_runtime.h>

#define GB (1024ULL * 1024 * 1024)
#define HOLE_G 36ULL
#define CHK(x) do { \
    cudaError_t _e = (x); \
    if (_e != cudaSuccess) { \
        fprintf(stderr, "FAIL %s: %s\n", #x, cudaGetErrorString(_e)); \
        return 1; \
    } \
} while (0)

static int check_range(void *p, size_t bytes, unsigned char expect, const char *tag) {
    const size_t chunk = 4 * 1024 * 1024;
    unsigned char *host = malloc(chunk);
    if (!host) return -1;
    size_t bad = 0;
    for (size_t off = 0; off < bytes; off += chunk) {
        size_t n = bytes - off < chunk ? bytes - off : chunk;
        if (cudaMemcpy(host, (char *)p + off, n, cudaMemcpyDeviceToHost) != cudaSuccess) {
            free(host);
            fprintf(stderr, "%s: memcpy fail @ off %zu\n", tag, off);
            return -1;
        }
        for (size_t i = 0; i < n; i++)
            if (host[i] != expect) bad++;
    }
    free(host);
    printf("  %s: expect 0x%02X bad_bytes=%zu / %zu %s\n",
           tag, expect, bad, bytes, bad ? "FAIL" : "OK");
    return bad ? 1 : 0;
}

static int test_alias_one_blob(void) {
    void *p = NULL;
    size_t sz = 55 * GB;
    printf("\n=== TEST A: single 55G blob (spans hole) — expect alias ===\n");
    CHK(cudaMalloc(&p, sz));
    CHK(cudaMemset(p, 0x11, sz));
    CHK(cudaMemset(p, 0xAA, HOLE_G * GB));
    CHK(cudaDeviceSynchronize());
    unsigned char b = 0;
    CHK(cudaMemcpy(&b, (char *)p + 41 * GB, 1, cudaMemcpyDeviceToHost));
    printf("  after rewrite [0,36G)->0xAA, byte@41G = 0x%02X (0x11=clean, 0xAA=aliased)\n", b);
    CHK(cudaFree(p));
    return (b == 0xAA) ? 0 : 1; /* 0 means alias demonstrated */
}

static int test_two_segments(void) {
    void *lo = NULL, *hi = NULL;
    size_t seg = 34 * GB; /* each segment stays on one side of hole */
    printf("\n=== TEST B: two separate 34G allocs — expect NO crosstalk ===\n");
    CHK(cudaMalloc(&lo, seg));
    CHK(cudaMalloc(&hi, seg));
    printf("  lo=%p hi=%p (delta %.2f GiB)\n", lo, hi, (hi - lo) / (double)GB);
    CHK(cudaMemset(lo, 0xAA, seg));
    CHK(cudaMemset(hi, 0xBB, seg));
    CHK(cudaDeviceSynchronize());
    int r0 = check_range(lo, seg, 0xAA, "lo segment");
    int r1 = check_range(hi, seg, 0xBB, "hi segment");
    /* rewrite lo only, hi must stay BB */
    CHK(cudaMemset(lo, 0xCC, 1024 * 1024)); /* touch first 1MiB of lo */
    CHK(cudaDeviceSynchronize());
    int r2 = check_range(hi, seg, 0xBB, "hi after lo touch");
    unsigned char hb = 0;
    CHK(cudaMemcpy(&hb, hi, 1, cudaMemcpyDeviceToHost));
    CHK(cudaFree(lo));
    CHK(cudaFree(hi));
    return (r0 || r1 || r2) ? 1 : 0;
}

static int child_hold(size_t gib, unsigned char pat) {
    size_t n = gib * GB;
    void *p = NULL;
    if (cudaSetDevice(0) != cudaSuccess) _exit(2);
    if (cudaMalloc(&p, n) != cudaSuccess) _exit(3);
    if (cudaMemset(p, pat, n) != cudaSuccess) _exit(4);
    cudaDeviceSynchronize();
    /* sample */
    unsigned char h[4] = {0};
    cudaMemcpy(h, p, 4, cudaMemcpyDeviceToHost);
    printf("child %zuG pat=0x%02X sample=0x%02X pid=%d\n", gib, pat, h[0], getpid());
    fflush(stdout);
    sleep(300); /* hold for parent to inspect */
    cudaFree(p);
    _exit(0);
}

static int test_dual_process(void) {
    printf("\n=== TEST C: dual process 34G+34G (~68G) different patterns ===\n");
    pid_t a = fork();
    if (a == 0) child_hold(34, 0xAA);
    pid_t b = fork();
    if (b == 0) child_hold(34, 0xBB);
    sleep(8);
    int fail = 0;
    FILE *fp = popen("nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits", "r");
    if (fp) {
        unsigned long mem = 0;
        if (fscanf(fp, "%lu", &mem) == 1)
            printf("  nvidia-smi used: %lu MiB (expect >>40960 if >40G real)\n", mem);
        if (mem < 60000) fail = 1;
        pclose(fp);
    }
    kill(a, SIGTERM);
    kill(b, SIGTERM);
    waitpid(a, NULL, 0);
    waitpid(b, NULL, 0);
    return fail;
}

static void meminfo(const char *tag) {
    size_t free = 0, total = 0;
    cudaMemGetInfo(&free, &total);
    printf("  [%s] cuda total=%.2f GiB free=%.2f GiB\n",
           tag, total / (double)GB, free / (double)GB);
}

int main(void) {
    int fail = 0;
    printf("=== prove_80g: physical capacity vs PTE alias ===\n");

    /* fork before any CUDA init in parent */
    if (test_dual_process() != 0) {
        printf("  => TEST C: FAIL total used too low\n");
        fail = 1;
    } else
        printf("  => TEST C: simultaneous ~68G+ holds (>>40G)\n");

    CHK(cudaSetDevice(0));
    CHK(cudaFree(0));
    meminfo("idle");

    if (test_alias_one_blob() == 0)
        printf("  => TEST A: +32G alias CONFIRMED on single跨洞 blob\n");
    else
        printf("  => TEST A: unexpected (no alias on 55G blob)\n");

    if (test_two_segments() == 0)
        printf("  => TEST B: two segments INDEPENDENT (68G distinct data OK)\n");
    else {
        printf("  => TEST B: FAIL crosstalk\n");
        fail = 1;
    }

    meminfo("after B");

    printf("\n=== SUMMARY ===\n");
    printf("  Physical ~80G: %s\n", fail ? "INCONCLUSIVE/FAIL" : "SUPPORTED");
    printf("  PTE +32G alias on ONE跨洞 mapping: see TEST A\n");
    printf("  Separate allocs can use >40G without pattern mix: see TEST B/C\n");
    return fail ? 1 : 0;
}
