/*
 * mmio_dump: bulk-read a BAR0 range via /sys/.../resource0 mmap.
 * usage: mmio_dump <start_hex> <end_hex>   (end exclusive, 4-byte words)
 * output: one "0x<offset> 0x<value>" per line to stdout.
 */
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/mman.h>

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: mmio_dump <start_hex> <end_hex>\n"); return 2; }
    uint64_t start = strtoull(argv[1], 0, 0);
    uint64_t end   = strtoull(argv[2], 0, 0);
    if (end <= start || end > 16 * 1024 * 1024) { fprintf(stderr, "bad range\n"); return 2; }
    int fd = open("/sys/bus/pci/devices/0000:09:00.0/resource0", O_RDWR | O_SYNC);
    if (fd < 0) { perror("open"); return 1; }
    void *p = mmap(0, 16 * 1024 * 1024, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }
    for (uint64_t off = start; off < end; off += 4) {
        volatile uint32_t v = *(volatile uint32_t *)((char *)p + off);
        printf("0x%08llx 0x%08x\n", (unsigned long long)off, v);
    }
    return 0;
}
