/*
 * wall_reconfirm.c - host-side 32G-wall confirmation tool.
 *
 * The tool deliberately uses pinned host staging plus synchronous
 * cudaMemcpy() instead of a device kernel.  That makes the data pattern and
 * every comparison visible on the host, and keeps the test useful even when
 * the suspected failure is in the address translation path.
 *
 * Modes:
 *   single <GiB>  one object, pat(offset) write/readback
 *   cross48       one 48-GiB object: low L, high H, then verify both
 *   canary        8-GiB canary, 28-GiB filler, 20-GiB high object
 *
 * All offsets used in the pattern are logical byte offsets from the start of
 * the object.  The high mode adds HIGH_MARK so high-to-low overwrites are
 * distinguishable from an ordinary pattern collision.
 */

#include <cuda.h>
#include <cuda_runtime.h>

#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#define GB (1024ULL * 1024ULL * 1024ULL)
#define CHUNK (256ULL * 1024ULL * 1024ULL)
#define HIGH_MARK UINT64_C(0x8000000000000000)
#define ALIAS_DELTA (32ULL * GB)
#define MOD_DELTA (4ULL * GB)
#define MAX_SAMPLES 400
#define SAMPLE_WINDOW_SHIFT 28 /* first bad qword per 256 MiB window */

typedef enum {
    MODE_LOW = 0,
    MODE_HIGH = 1,
} PatternMode;

typedef struct {
    uint64_t offset;
    uint64_t expected;
    uint64_t actual;
    uint64_t h1_minus32g_low;
    uint64_t h1_minus32g_high;
    uint64_t h1_plus32g_low;
    uint64_t h1_plus32g_high;
    uint64_t h2_mod4g_low;
    uint64_t h2_mod4g_high;
    const char *tag;
} Sample;

typedef struct {
    const char *name;
    uint64_t bytes;
    uint64_t *bucket_bad;
    size_t bucket_count;
    uint64_t bad_qwords;
    uint64_t h1_minus32g_low;
    uint64_t h1_minus32g_high;
    uint64_t h1_plus32g_low;
    uint64_t h1_plus32g_high;
    uint64_t h2_mod4g_low;
    uint64_t h2_mod4g_high;
    uint64_t h3_other;
    uint64_t last_window;
    Sample samples[MAX_SAMPLES];
    size_t sample_count;
} VerifyStats;

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static uint64_t pattern(uint64_t addr) {
    return addr * UINT64_C(0x9E3779B97F4A7C15) ^ (addr >> 3);
}

static uint64_t expected_at(uint64_t offset, PatternMode mode) {
    uint64_t v = pattern(offset);
    return mode == MODE_HIGH ? (v | HIGH_MARK) : v;
}

static int cuda_error(const char *expr, cudaError_t err, const char *file,
                      int line) {
    fprintf(stderr, "CUDA FAIL %s at %s:%d: %s (%d)\n", expr, file, line,
            cudaGetErrorString(err), (int)err);
    return 1;
}

#define CUDA_OK(expr)                                                        \
    do {                                                                     \
        cudaError_t _err = (expr);                                           \
        if (_err != cudaSuccess)                                             \
            return cuda_error(#expr, _err, __FILE__, __LINE__);              \
    } while (0)

static int driver_error(const char *expr, CUresult err, const char *file,
                        int line) {
    const char *name = NULL;
    const char *desc = NULL;
    cuGetErrorName(err, &name);
    cuGetErrorString(err, &desc);
    fprintf(stderr, "DRIVER FAIL %s at %s:%d: %s: %s (%d)\n", expr, file,
            line, name ? name : "unknown", desc ? desc : "unknown",
            (int)err);
    return 1;
}

#define DRIVER_OK(expr)                                                      \
    do {                                                                     \
        CUresult _err = (expr);                                              \
        if (_err != CUDA_SUCCESS)                                            \
            return driver_error(#expr, _err, __FILE__, __LINE__);            \
    } while (0)

static int init_cuda(void) {
    int count = 0;
    CUDA_OK(cudaGetDeviceCount(&count));
    if (count < 1) {
        fprintf(stderr, "No CUDA device found\n");
        return 1;
    }
    CUDA_OK(cudaSetDevice(0));
    CUDA_OK(cudaFree(0));
    DRIVER_OK(cuInit(0));
    return 0;
}

static int print_object(const char *name, void *ptr, uint64_t bytes) {
    CUdeviceptr base = 0;
    size_t range = 0;
    DRIVER_OK(cuMemGetAddressRange(&base, &range,
                                   (CUdeviceptr)(uintptr_t)ptr));
    uint64_t end = (uint64_t)base + bytes;
    printf("OBJECT %s ptr=%p bytes=%" PRIu64 " (%.2f GiB)\n", name, ptr,
           bytes, (double)bytes / (double)GB);
    printf("  VA base=0x%016" PRIx64 " range=%zu (%.2f GiB) end=0x%016" PRIx64
           " base%%1G=0x%08" PRIx64 "\n",
           (uint64_t)base, range, (double)range / (double)GB, end,
           (uint64_t)((uint64_t)base % (uint64_t)GB));
    fflush(stdout);
    return 0;
}

static void print_progress(const char *name, uint64_t off, uint64_t total,
                           double start) {
    printf("  %s %.2f/%.2f GiB (%.1f%%, %.1f s)\n", name,
           (double)off / (double)GB, (double)total / (double)GB,
           total ? 100.0 * (double)off / (double)total : 100.0,
           now_s() - start);
    fflush(stdout);
}

static int alloc_staging(uint64_t **host) {
    cudaError_t err = cudaHostAlloc((void **)host, CHUNK, cudaHostAllocPortable);
    if (err != cudaSuccess) {
        fprintf(stderr, "Pinned host staging allocation failed: %s (%d)\n",
                cudaGetErrorString(err), (int)err);
        return 1;
    }
    return 0;
}

static void free_staging(uint64_t *host) {
    if (host)
        cudaFreeHost(host);
}

static int write_range(void *dev, uint64_t start, uint64_t bytes,
                       PatternMode mode, const char *tag, uint64_t *host) {
    double t0 = now_s();
    printf("WRITE %s range=[%.2f,%.2f) GiB mode=%s\n", tag,
           (double)start / (double)GB, (double)(start + bytes) / (double)GB,
           mode == MODE_HIGH ? "H=pat|HIGH_MARK" : "L=pat");
    fflush(stdout);

    for (uint64_t off = 0; off < bytes;) {
        uint64_t n = bytes - off < CHUNK ? bytes - off : CHUNK;
        size_t qwords = (size_t)(n / sizeof(uint64_t));
        uint64_t logical = start + off;
        for (size_t i = 0; i < qwords; ++i)
            host[i] = expected_at(logical + (uint64_t)i * sizeof(uint64_t), mode);
        CUDA_OK(cudaMemcpy((char *)dev + logical, host, (size_t)n,
                           cudaMemcpyHostToDevice));
        off += n;
        if (off == bytes || (off % (4ULL * GB)) == 0)
            print_progress(tag, start + off, start + bytes, t0);
    }
    CUDA_OK(cudaDeviceSynchronize());
    printf("WRITE %s done in %.1f s\n", tag, now_s() - t0);
    fflush(stdout);
    return 0;
}

static void classify_sample(VerifyStats *stats, uint64_t offset,
                            uint64_t expected, uint64_t actual,
                            PatternMode mode, const char *tag) {
    (void)mode;
    uint64_t minus32_low = offset >= ALIAS_DELTA
                               ? pattern(offset - ALIAS_DELTA)
                               : UINT64_MAX;
    uint64_t minus32_high = minus32_low == UINT64_MAX
                                ? UINT64_MAX
                                : (minus32_low | HIGH_MARK);
    uint64_t plus32_low = pattern(offset + ALIAS_DELTA);
    uint64_t plus32_high = plus32_low | HIGH_MARK;
    uint64_t mod4_low = pattern(offset % MOD_DELTA);
    uint64_t mod4_high = mod4_low | HIGH_MARK;
    int matched = 0;
    if (actual == minus32_low) {
        stats->h1_minus32g_low++;
        matched = 1;
    }
    if (actual == minus32_high) {
        stats->h1_minus32g_high++;
        matched = 1;
    }
    if (actual == plus32_low) {
        stats->h1_plus32g_low++;
        matched = 1;
    }
    if (actual == plus32_high) {
        stats->h1_plus32g_high++;
        matched = 1;
    }
    if (actual == mod4_low) {
        stats->h2_mod4g_low++;
        matched = 1;
    }
    if (actual == mod4_high) {
        stats->h2_mod4g_high++;
        matched = 1;
    }
    if (!matched)
        stats->h3_other++;
    uint64_t window = offset >> SAMPLE_WINDOW_SHIFT;
    if (stats->sample_count < 5 ||
        (window != stats->last_window &&
         stats->sample_count < MAX_SAMPLES)) {
        stats->last_window = window;
        Sample *s = &stats->samples[stats->sample_count++];
        s->offset = offset;
        s->expected = expected;
        s->actual = actual;
        s->h1_minus32g_low = minus32_low;
        s->h1_minus32g_high = minus32_high;
        s->h1_plus32g_low = plus32_low;
        s->h1_plus32g_high = plus32_high;
        s->h2_mod4g_low = mod4_low;
        s->h2_mod4g_high = mod4_high;
        s->tag = tag;
    }
}

static int verify_range(void *dev, uint64_t start, uint64_t bytes,
                        PatternMode mode, const char *name,
                        VerifyStats *stats, uint64_t *host) {
    double t0 = now_s();
    memset(stats, 0, sizeof(*stats));
    stats->name = name;
    stats->bytes = bytes;
    stats->bucket_count = (size_t)((bytes + GB - 1) / GB);
    stats->bucket_bad = calloc(stats->bucket_count, sizeof(uint64_t));
    if (!stats->bucket_bad) {
        fprintf(stderr, "calloc histogram failed for %zu buckets\n",
                stats->bucket_count);
        return 1;
    }

    printf("VERIFY %s range=[%.2f,%.2f) GiB mode=%s\n", name,
           (double)start / (double)GB,
           (double)(start + bytes) / (double)GB,
           mode == MODE_HIGH ? "H=pat|HIGH_MARK" : "L=pat");
    fflush(stdout);

    for (uint64_t off = 0; off < bytes;) {
        uint64_t n = bytes - off < CHUNK ? bytes - off : CHUNK;
        size_t qwords = (size_t)(n / sizeof(uint64_t));
        CUDA_OK(cudaMemcpy(host, (char *)dev + start + off, (size_t)n,
                           cudaMemcpyDeviceToHost));
        for (size_t i = 0; i < qwords; ++i) {
            uint64_t logical = start + off + (uint64_t)i * sizeof(uint64_t);
            uint64_t expected = expected_at(logical, mode);
            uint64_t actual = host[i];
            if (actual == expected)
                continue;
            stats->bad_qwords++;
            size_t bucket = (size_t)(logical / GB);
            if (bucket < stats->bucket_count)
                stats->bucket_bad[bucket]++;
            classify_sample(stats, logical, expected, actual, mode, name);
        }
        off += n;
        if (off == bytes || (off % (4ULL * GB)) == 0)
            print_progress(name, start + off, start + bytes, t0);
    }
    CUDA_OK(cudaDeviceSynchronize());
    printf("VERIFY %s done in %.1f s bad_qwords=%" PRIu64 "\n", name,
           now_s() - t0, stats->bad_qwords);
    for (size_t i = 0; i < stats->bucket_count; ++i)
        if (stats->bucket_bad[i])
            printf("  bucket %2zuG..%2zuG bad_qwords=%" PRIu64 "\n", i,
                   i + 1, stats->bucket_bad[i]);
    printf("  hypotheses: H1-=pat(addr-32G)=%" PRIu64
           " H1-=pat(addr-32G)|MARK=%" PRIu64
           " H1+=pat(addr+32G)=%" PRIu64
           " H1+=pat(addr+32G)|MARK=%" PRIu64
           " H2=pat(addr%%4G)=%" PRIu64
           " H2=pat(addr%%4G)|MARK=%" PRIu64
           " H3-other=%" PRIu64 "\n",
           stats->h1_minus32g_low, stats->h1_minus32g_high,
           stats->h1_plus32g_low, stats->h1_plus32g_high,
           stats->h2_mod4g_low, stats->h2_mod4g_high, stats->h3_other);
    for (size_t i = 0; i < stats->sample_count; ++i) {
        const Sample *s = &stats->samples[i];
        printf("  sample[%zu] %s addr=0x%016" PRIx64
               " expected=0x%016" PRIx64 " actual=0x%016" PRIx64
               " H1-L-=0x%016" PRIx64 " H1-H-=0x%016" PRIx64
               " H1-L+=0x%016" PRIx64 " H1-H+=0x%016" PRIx64
               " H2-L=0x%016" PRIx64 " H2-H=0x%016" PRIx64 "\n",
               i, s->tag, s->offset, s->expected, s->actual,
               s->h1_minus32g_low, s->h1_minus32g_high,
               s->h1_plus32g_low, s->h1_plus32g_high, s->h2_mod4g_low,
               s->h2_mod4g_high);
    }
    fflush(stdout);
    return 0;
}

static void free_stats(VerifyStats *stats) {
    free(stats->bucket_bad);
    stats->bucket_bad = NULL;
}

static int run_single(uint64_t gib, const char *label) {
    uint64_t bytes = gib * GB;
    void *dev = NULL;
    uint64_t *host = NULL;
    VerifyStats stats;
    int rc = 1;

    printf("=== SINGLE %s %" PRIu64 " GiB ===\n", label, gib);
    CUDA_OK(cudaMalloc(&dev, (size_t)bytes));
    if (print_object(label, dev, bytes) || alloc_staging(&host))
        goto out;
    if (write_range(dev, 0, bytes, MODE_LOW, label, host))
        goto out;
    if (verify_range(dev, 0, bytes, MODE_LOW, label, &stats, host))
        goto out;
    rc = stats.bad_qwords ? 2 : 0;
    free_stats(&stats);
out:
    free_staging(host);
    if (dev)
        cudaFree(dev);
    printf("=== SINGLE %s RESULT=%s ===\n", label,
           rc == 0 ? "PASS" : (rc == 2 ? "FAIL_DATA" : "FAIL_RUNTIME"));
    fflush(stdout);
    return rc;
}

static int run_cross48(void) {
    const uint64_t bytes = 48 * GB;
    const uint64_t low = 32 * GB;
    const uint64_t high = 16 * GB;
    void *dev = NULL;
    uint64_t *host = NULL;
    VerifyStats low_before_stats, low_stats, high_stats;
    int have_low_before = 0;
    int have_low = 0;
    int have_high = 0;
    int rc = 1;

    printf("=== CROSS48 object内交叉污染: low=32G high=16G ===\n");
    CUDA_OK(cudaMalloc(&dev, (size_t)bytes));
    if (print_object("cross48", dev, bytes) || alloc_staging(&host))
        goto out;
    if (write_range(dev, 0, low, MODE_LOW, "cross48-low-L", host))
        goto out;
    if (verify_range(dev, 0, low, MODE_LOW, "cross48-low-before-H",
                     &low_before_stats, host))
        goto out;
    have_low_before = 1;
    if (write_range(dev, low, high, MODE_HIGH, "cross48-high-H", host))
        goto out;
    if (verify_range(dev, 0, low, MODE_LOW, "cross48-low-after-H", &low_stats,
                     host))
        goto out;
    have_low = 1;
    if (verify_range(dev, low, high, MODE_HIGH, "cross48-high", &high_stats,
                     host))
        goto out;
    have_high = 1;
    rc = (low_stats.bad_qwords || high_stats.bad_qwords) ? 2 : 0;
out:
    if (have_low_before)
        free_stats(&low_before_stats);
    if (have_low)
        free_stats(&low_stats);
    if (have_high)
        free_stats(&high_stats);
    free_staging(host);
    if (dev)
        cudaFree(dev);
    printf("=== CROSS48 RESULT=%s ===\n",
           rc == 0 ? "PASS_NO_CROSS污染" :
           (rc == 2 ? "FAIL_CROSS污染_OR_DATA" : "FAIL_RUNTIME"));
    fflush(stdout);
    return rc;
}

static int run_canary(int reverse_va_order, uint64_t filler_gib) {
    const uint64_t canary_bytes = 8 * GB;
    const uint64_t filler_bytes = filler_gib * GB;
    const uint64_t high_bytes = 20 * GB;
    void *canary = NULL;
    void *filler = NULL;
    void *high = NULL;
    uint64_t *host = NULL;
    VerifyStats c_before_stats, cstats, hstats;
    int have_c_before = 0;
    int have_c = 0;
    int have_h = 0;
    int rc = 1;

    printf("=== CANARY cross-object: C=8G filler=%" PRIu64
           "G H=20G (%s) ===\n",
           filler_gib,
           reverse_va_order ? "H-first allocation for ascending VA order"
                            : "document allocation order C-first");
    if (reverse_va_order) {
        CUDA_OK(cudaMalloc(&high, (size_t)high_bytes));
        CUDA_OK(cudaMalloc(&filler, (size_t)filler_bytes));
        CUDA_OK(cudaMalloc(&canary, (size_t)canary_bytes));
    } else {
        CUDA_OK(cudaMalloc(&canary, (size_t)canary_bytes));
        CUDA_OK(cudaMalloc(&filler, (size_t)filler_bytes));
        CUDA_OK(cudaMalloc(&high, (size_t)high_bytes));
    }
    if (print_object("canary-C", canary, canary_bytes) ||
        print_object("filler", filler, filler_bytes) ||
        print_object("high-H", high, high_bytes) || alloc_staging(&host))
        goto out;
    if (write_range(canary, 0, canary_bytes, MODE_LOW, "canary-C", host))
        goto out;
    if (write_range(filler, 0, filler_bytes, MODE_LOW, "filler", host))
        goto out;
    if (verify_range(canary, 0, canary_bytes, MODE_LOW,
                     "canary-C-after-filler-before-H", &c_before_stats,
                     host))
        goto out;
    have_c_before = 1;
    if (write_range(high, 0, high_bytes, MODE_HIGH, "high-H", host))
        goto out;
    if (verify_range(canary, 0, canary_bytes, MODE_LOW,
                     "canary-C-after-high-H", &cstats, host))
        goto out;
    have_c = 1;
    if (verify_range(high, 0, high_bytes, MODE_HIGH, "high-H-self", &hstats,
                     host))
        goto out;
    have_h = 1;
    rc = (cstats.bad_qwords || hstats.bad_qwords) ? 2 : 0;
out:
    if (have_c_before)
        free_stats(&c_before_stats);
    if (have_c)
        free_stats(&cstats);
    if (have_h)
        free_stats(&hstats);
    free_staging(host);
    if (high)
        cudaFree(high);
    if (filler)
        cudaFree(filler);
    if (canary)
        cudaFree(canary);
    printf("=== CANARY RESULT=%s ===\n",
           rc == 0 ? "PASS_NO_CROSS污染" :
           (rc == 2 ? "FAIL_CANARY污染_OR_DATA" : "FAIL_RUNTIME"));
    fflush(stdout);
    return rc;
}

static void usage(const char *argv0) {
    fprintf(stderr,
            "usage: %s single <GiB> [label] | cross48 | canary | "
            "canary-reverse | canary-clean\n",
            argv0);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        usage(argv[0]);
        return 64;
    }
    if (init_cuda())
        return 1;
    if (strcmp(argv[1], "single") == 0) {
        if (argc < 3) {
            usage(argv[0]);
            return 64;
        }
        char *end = NULL;
        errno = 0;
        uint64_t gib = strtoull(argv[2], &end, 0);
        if (errno || !end || *end || !gib || gib > 78) {
            fprintf(stderr, "invalid GiB size: %s\n", argv[2]);
            return 64;
        }
        return run_single(gib, argc >= 4 ? argv[3] : "single");
    }
    if (strcmp(argv[1], "cross48") == 0)
        return run_cross48();
    if (strcmp(argv[1], "canary") == 0)
        return run_canary(0, 28);
    if (strcmp(argv[1], "canary-reverse") == 0)
        return run_canary(1, 28);
    if (strcmp(argv[1], "canary-clean") == 0)
        return run_canary(1, 24);
    usage(argv[0]);
    return 64;
}
