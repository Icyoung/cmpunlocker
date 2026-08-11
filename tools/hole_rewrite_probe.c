/*
 * hole_rewrite_probe: dual-write / +32G aliasing smoke test.
 *
 * 1) cudaMalloc large buffer spanning phantom hole [36G,41G)
 * 2) Fill entire allocation with pattern A (0x11)
 * 3) Rewrite [0, 36G) with pattern B (0xAA) — only before the hole
 * 4) Sample reads after the hole — still A means no cross-band pollution
 *
 * If VA x aliases x+32G, writes at e.g. 9G can change bytes at 41G.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <cuda_runtime.h>

#define GB (1024ULL * 1024 * 1024)
#define HOLE_START_G 36ULL
#define AFTER_HOLE_G 41ULL
#define ALLOC_G 55ULL
#define PAT_A 0x11
#define PAT_B 0xAA
#define SAMPLE (1ULL * 1024 * 1024)

#define CHK(x) do { \
    cudaError_t _e = (x); \
    if (_e != cudaSuccess) { \
        fprintf(stderr, "FAIL %s: %s\n", #x, cudaGetErrorString(_e)); \
        return 1; \
    } \
} while (0)

static int sample_byte(void *base, size_t off, unsigned char *out) {
    return cudaMemcpy(out, (char *)base + off, 1, cudaMemcpyDeviceToHost) == cudaSuccess ? 0 : 1;
}

static int sample_stats(void *base, size_t off, size_t len, unsigned char expect,
                        unsigned long long *bad, unsigned char *first) {
    unsigned char *host = (unsigned char *)malloc(len);
    if (!host) return -1;
    if (cudaMemcpy(host, (char *)base + off, len, cudaMemcpyDeviceToHost) != cudaSuccess) {
        free(host);
        return -1;
    }
    unsigned long long nbad = 0;
    for (size_t i = 0; i < len; i++) {
        if (host[i] != expect) {
            if (nbad == 0) *first = host[i];
            nbad++;
        }
    }
    *bad = nbad;
    free(host);
    return 0;
}

static void report(const char *tag, size_t off_g, unsigned char expect) {
    printf("  %-28s @%2zuG: ", tag, off_g);
}

int main(void) {
    void *dev = NULL;
    size_t alloc = ALLOC_G * GB;
    size_t before = HOLE_START_G * GB;

    printf("[hole_rewrite_probe] alloc=%zuG hole=[%zuG,%zuG) fill=0x%02X rewrite_before=0x%02X\n",
           (size_t)ALLOC_G, (size_t)HOLE_START_G, (size_t)AFTER_HOLE_G, PAT_A, PAT_B);

    CHK(cudaSetDevice(0));
    CHK(cudaFree(0));
    CHK(cudaMalloc(&dev, alloc));
    printf("VA base = %p\n", dev);

    CHK(cudaMemset(dev, PAT_A, alloc));
    CHK(cudaDeviceSynchronize());
    printf("step1: filled entire alloc with 0x%02X\n", PAT_A);

    CHK(cudaMemset(dev, PAT_B, before));
    CHK(cudaDeviceSynchronize());
    printf("step2: rewrote [0,%zuG) with 0x%02X\n", (size_t)HOLE_START_G, PAT_B);

    printf("step3: samples (expect after-hole still 0x%02X unless +32G alias):\n", PAT_A);

    static const size_t probes[] = {1, 4, 9, 32, 35, 36, 41, 45, 50, 54};
    for (size_t i = 0; i < sizeof(probes) / sizeof(probes[0]); i++) {
        size_t g = probes[i];
        if (g >= ALLOC_G) continue;
        unsigned char b = 0;
        unsigned char expect = (g < HOLE_START_G) ? PAT_B : PAT_A;
        if (g >= HOLE_START_G && g < AFTER_HOLE_G) expect = PAT_A; /* hole not in alloc */
        if (g >= AFTER_HOLE_G) expect = PAT_A;

        if (sample_byte(dev, g * GB, &b) != 0) {
            printf("  @%2zuG: memcpy FAIL\n", g);
            continue;
        }
        const char *zone = g < HOLE_START_G ? "before" :
                           (g < AFTER_HOLE_G ? "hole-skip" : "after ");
        printf("  @%2zuG [%s]: got 0x%02X expect 0x%02X %s\n",
               g, zone, b, expect, (b == expect) ? "OK" : "*** MISMATCH ***");
    }

    printf("step4: 1MiB stats at key bands:\n");
    struct { const char *tag; size_t g; unsigned char exp; } bands[] = {
        {"before-hole tail", 35, PAT_B},
        {"after-hole head", 41, PAT_A},
        {"after-hole +4G", 45, PAT_A},
        {"after-hole +9G", 50, PAT_A},
    };
    int any_bad = 0;
    for (size_t i = 0; i < sizeof(bands) / sizeof(bands[0]); i++) {
        unsigned long long bad = 0;
        unsigned char first = 0;
        size_t off = bands[i].g * GB;
        if (sample_stats(dev, off, SAMPLE, bands[i].exp, &bad, &first) != 0) {
            printf("  %s: sample FAIL\n", bands[i].tag);
            any_bad = 1;
            continue;
        }
        printf("  %s @%zuG: bad=%llu / %llu %s",
               bands[i].tag, bands[i].g, bad, (unsigned long long)SAMPLE,
               bad ? "(CORRUPTED)" : "OK");
        if (bad) {
            printf(" first=0x%02X", first);
            any_bad = 1;
        }
        printf("\n");
    }

    /* +32G pair check: write was at 9G -> alias target 41G */
    unsigned char at9 = 0, at41 = 0;
    sample_byte(dev, 9 * GB, &at9);
    sample_byte(dev, 41 * GB, &at41);
    printf("step5: +32G pair 9G=0x%02X 41G=0x%02X %s\n",
           at9, at41,
           (at9 == PAT_B && at41 == PAT_B) ? "*** 9G write visible at 41G (+32G alias) ***" :
           (at41 == PAT_A ? "41G still A (no alias from 9G)" : "unexpected"));

    printf("\nRESULT: %s\n", any_bad ? "AFTER-HOLE CORRUPTION DETECTED" : "after-hole clean");
    CHK(cudaFree(dev));
    return any_bad ? 2 : 0;
}
