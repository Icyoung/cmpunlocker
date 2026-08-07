/*
 * f0_size_probe: is the GSP death triggered by the big single-launch SM
 * write itself, or by the free/teardown afterwards?
 *
 * Zone probe (2026-08-07) proved: any 20G zone of a 78G alloc writes at
 * full bandwidth.  Ladder showed: 62G single-launch write in a 78G alloc
 * kills GSP in ~1s.  This probe walks single-launch write sizes up from
 * a safe 60G, and after each write it runs f0_probe BEFORE child exit
 * (implicit cudaFree at process teardown) and again AFTER — separating
 * "write killed it" from "free killed it".
 *
 * Parent/child sync via pipe: child prints stage markers to stdout and
 * signals the parent through the pipe after the write; parent probes,
 * then lets the child exit.
 *
 * usage: f0_size_probe [startGB endGB stepGB]   (default 60 64 1)
 * env:   ALLOC_GB (default 78)
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

/* Self-locating fill: each qword gets  magic | (qword_index << 3) | 0b110.
 * The low 0b110 keeps the value 8-misaligned, so if GSP dereferences a
 * pointer slot inside our allocation, mcause=4 fires and mbadaddr carries
 * the slot's own offset: byte_offset = (mbadaddr >> 3) & 0x1FFFFFFFFF) * 8.
 * Turns the crash into a coordinate readout for the phantom structure. */
__global__ void fill_selfloc_kernel(uint64_t *p, size_t n_qwords) {
    size_t i = (size_t)blockIdx.x * blockDim.x + threadIdx.x;
    size_t stride = (size_t)gridDim.x * blockDim.x;
    for (; i < n_qwords; i += stride)
        p[i] = 0x5A5A000000000006ULL | ((uint64_t)i << 3);
}

static double ts_now(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

static void run_host_probe(const char *tag, uint64_t n) {
    printf("[size %lluG] host probe %s: ", (unsigned long long)n, tag);
    fflush(stdout);
    int rc = system("/home/icy/f0/f0_probe 2>&1 | tail -1");
    printf("[size %lluG] host probe %s rc=%d\n", (unsigned long long)n, tag, rc);
    fflush(stdout);
}

static int child_run(uint64_t alloc_gb, uint64_t write_gb, int sync_fd) {
    if (cudaSetDevice(0) != cudaSuccess) return 10;
    if (cudaFree(0) != cudaSuccess) return 11;
    uint32_t *dev = NULL;
    double t0 = ts_now();
    if (cudaMalloc((void**)&dev, (size_t)alloc_gb * GB) != cudaSuccess) return 12;
    printf("[size %lluG] child: alloc %lluG OK (%.2fs)\n",
           (unsigned long long)write_gb, (unsigned long long)alloc_gb, ts_now() - t0);
    fflush(stdout);

    t0 = ts_now();
    if (getenv("SELFLOC")) {
        fill_selfloc_kernel<<<2048, 256>>>((uint64_t*)dev, (size_t)write_gb * GB / 8);
    } else {
        fill_kernel<<<2048, 256>>>(dev, (size_t)write_gb * GB / 4, 0x5A5A5A5Au);
    }
    cudaError_t e = cudaDeviceSynchronize();
    printf("[size %lluG] child: single-launch write %s (%.3fs)\n",
           (unsigned long long)write_gb,
           e == cudaSuccess ? "OK" : cudaGetErrorString(e), ts_now() - t0);
    fflush(stdout);
    if (e != cudaSuccess) return 13;

    /* signal parent: write done, alloc still held */
    if (write(sync_fd, "W", 1) != 1) return 14;
    /* optional watch window: hold the alloc alive so parent can probe
       whether GSP dies AFTER the write without any free */
    int watch = getenv("WATCH_SECS") ? atoi(getenv("WATCH_SECS")) : 0;
    if (watch > 0) {
        printf("[size %lluG] child: holding alloc for %ds watch window\n",
               (unsigned long long)write_gb, watch);
        fflush(stdout);
        sleep(watch);
    }
    /* wait for parent to finish probing; parent closes pipe -> read EOF */
    char b;
    while (read(sync_fd, &b, 1) > 0) {}
    printf("[size %lluG] child: exiting (implicit free)\n", (unsigned long long)write_gb);
    fflush(stdout);
    return 0;   /* process teardown frees the CUDA context */
}

int main(int argc, char **argv) {
    uint64_t start = 60, end = 64, step = 1;
    if (argc > 1) start = strtoull(argv[1], 0, 0);
    if (argc > 2) end   = strtoull(argv[2], 0, 0);
    if (argc > 3) step  = strtoull(argv[3], 0, 0);
    uint64_t alloc_gb = getenv("ALLOC_GB") ? strtoull(getenv("ALLOC_GB"), 0, 0) : 78;

    printf("[size_probe] single-launch SM write ladder %lluG..%lluG step %lluG in %lluG alloc\n",
           (unsigned long long)start, (unsigned long long)end,
           (unsigned long long)step, (unsigned long long)alloc_gb);
    fflush(stdout);

    for (uint64_t n = start; n <= end; n += step) {
        int fds[2];
        if (pipe(fds) != 0) return 9;
        printf("[size_probe] === %lluG ===\n", (unsigned long long)n);
        fflush(stdout);
        pid_t pid = fork();
        if (pid == 0) {
            close(fds[0]);
            _exit(child_run(alloc_gb, n, fds[1]));
        }
        close(fds[1]);
        /* wait for 'W' or EOF (child died before signaling) */
        char b; int got = 0;
        double t0 = ts_now();
        while (ts_now() - t0 < 120) {
            ssize_t r = read(fds[0], &b, 1);
            if (r == 1) { got = 1; break; }
            if (r == 0) break;  /* EOF: child exited without signal */
            usleep(50000);
        }
        if (got) {
            run_host_probe("post-write-pre-free", n);
            int watch = getenv("WATCH_SECS") ? atoi(getenv("WATCH_SECS")) : 0;
            for (int w = 0; w < watch; w += 10) {
                sleep(watch - w < 10 ? watch - w : 10);
                run_host_probe("watch", n);
            }
        } else {
            printf("[size %lluG] no write signal (child died mid-write?)\n",
                   (unsigned long long)n);
        }
        close(fds[0]);  /* release child to exit */
        int status = 0;
        t0 = ts_now();
        while (ts_now() - t0 < 60) {
            pid_t r = waitpid(pid, &status, WNOHANG);
            if (r == pid) break;
            usleep(100000);
        }
        run_host_probe("post-free", n);
        int rc = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
        printf("[size_probe] %lluG child status=%d\n", (unsigned long long)n, rc);
        fflush(stdout);
        if (rc != 0) {
            printf("[size_probe] child failed at %lluG; STOPPING\n", (unsigned long long)n);
            return 3;
        }
    }
    printf("[size_probe] ALL SIZES PASS\n");
    return 0;
}
