/* mmio_rw: read or write specific BAR0 offsets.
 * usage: mmio_rw <bdf> r <off>...        read
 *        mmio_rw <bdf> w <off> <val>     write then read back
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/mman.h>

int main(int argc, char **argv) {
    if (argc < 4) { fprintf(stderr, "usage: mmio_rw <bdf> r|w ...\n"); return 2; }
    char path[256];
    snprintf(path, sizeof path, "/sys/bus/pci/devices/%s/resource0", argv[1]);
    int fd = open(path, O_RDWR | O_SYNC);
    if (fd < 0) { perror("open"); return 1; }
    void *p = mmap(0, 16*1024*1024, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }
    if (!strcmp(argv[2], "r")) {
        for (int i = 3; i < argc; i++) {
            uint64_t off = strtoull(argv[i], 0, 0);
            volatile uint32_t v = *(volatile uint32_t *)((char *)p + off);
            printf("0x%08llx 0x%08x\n", (unsigned long long)off, v);
        }
    } else if (!strcmp(argv[2], "w")) {
        if (argc != 5) { fprintf(stderr, "w needs <off> <val>\n"); return 2; }
        uint64_t off = strtoull(argv[3], 0, 0);
        uint32_t val = (uint32_t)strtoull(argv[4], 0, 0);
        volatile uint32_t *reg = (volatile uint32_t *)((char *)p + off);
        uint32_t before = *reg;
        *reg = val;
        __sync_synchronize();
        uint32_t after = *reg;
        printf("0x%08llx before=0x%08x wrote=0x%08x after=0x%08x %s\n",
               (unsigned long long)off, before, val, after,
               after == val ? "STICKS" : (after == before ? "REJECTED" : "CHANGED"));
    } else {
        fprintf(stderr, "unknown mode %s\n", argv[2]);
        return 2;
    }
    return 0;
}
