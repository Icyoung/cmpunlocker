/* mmio_list: read specific BAR0 offsets. usage: mmio_list <bdf> off1 off2 ... */
#include <stdio.h>
#include <stdlib.h>
#include <fcntl.h>
#include <stdint.h>
#include <unistd.h>
#include <sys/mman.h>
int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: mmio_list <bdf> off...\n"); return 2; }
    char path[256];
    snprintf(path, sizeof path, "/sys/bus/pci/devices/%s/resource0", argv[1]);
    int fd = open(path, O_RDWR | O_SYNC);
    if (fd < 0) { perror("open"); return 1; }
    void *p = mmap(0, 16*1024*1024, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) { perror("mmap"); return 1; }
    for (int i = 2; i < argc; i++) {
        uint64_t off = strtoull(argv[i], 0, 0);
        volatile uint32_t v = *(volatile uint32_t *)((char *)p + off);
        printf("0x%08llx 0x%08x\n", (unsigned long long)off, v);
    }
    return 0;
}
