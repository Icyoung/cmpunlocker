#!/usr/bin/env bash
# dual_34g_verify.sh — two processes, 34G each, AA vs BB, sample 1MiB while both live
set -euo pipefail
CU13=/home/icy/vllm-env/.venv/lib/python3.12/site-packages/nvidia/cu13
export LD_LIBRARY_PATH=${LD_LIBRARY_PATH:-}:/home/icy/cmpunlocker/f0/libs:$CU13/lib

cat > /tmp/dual_34g_child.c <<'EOF'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <cuda_runtime.h>
#define GB (1024ULL*1024*1024)
#define CHK(x) do{cudaError_t e=(x);if(e){fprintf(stderr,"%s: %s\n",#x,cudaGetErrorString(e));return 2;}}while(0)
int main(int argc, char **argv) {
    if (argc < 3) return 1;
    int pat = (int)strtol(argv[1], NULL, 16);
    const char *tag = argv[2];
    void *p = NULL;
    CHK(cudaSetDevice(0));
    CHK(cudaMalloc(&p, 34*GB));
    CHK(cudaMemset(p, pat, 34*GB));
    CHK(cudaDeviceSynchronize());
    unsigned char *host = malloc(1024*1024);
    if (!host) return 3;
    CHK(cudaMemcpy(host, p, 1024*1024, cudaMemcpyDeviceToHost));
    size_t bad = 0;
    for (size_t i = 0; i < 1024*1024; i++) if (host[i] != (unsigned char)pat) bad++;
    printf("%s pid=%d ptr=%p pat=0x%02X sample1MiB bad=%zu %s\n",
           tag, getpid(), p, pat, bad, bad ? "FAIL" : "OK");
    fflush(stdout);
    /* hold 120s for peer + smi */
    sleep(120);
    /* re-sample before exit */
    CHK(cudaMemcpy(host, p, 1024*1024, cudaMemcpyDeviceToHost));
    bad = 0;
    for (size_t i = 0; i < 1024*1024; i++) if (host[i] != (unsigned char)pat) bad++;
    printf("%s pid=%d RESAMPLE bad=%zu %s\n", tag, getpid(), bad, bad ? "FAIL" : "OK");
    free(host);
    cudaFree(p);
    return bad ? 4 : 0;
}
EOF

gcc -O2 -o /tmp/dual_34g_child /tmp/dual_34g_child.c \
  -I"$CU13/include" "$CU13/lib/libcudart.so.13" -Wl,-rpath,"$CU13/lib"

pkill -x dual_34g_child 2>/dev/null || true
sleep 1
rm -f /tmp/dual_34g_a.log /tmp/dual_34g_b.log

/tmp/dual_34g_child AA left  > /tmp/dual_34g_a.log 2>&1 &
PA=$!
/tmp/dual_34g_child BB right > /tmp/dual_34g_b.log 2>&1 &
PB=$!

for i in $(seq 1 30); do
  if grep -q "sample1MiB" /tmp/dual_34g_a.log && grep -q "sample1MiB" /tmp/dual_34g_b.log; then
    break
  fi
  sleep 1
done

echo "=== nvidia-smi ==="
nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader
nvidia-smi | grep -E "dual_34g|MiB"

echo "=== child logs (initial sample) ==="
grep sample1MiB /tmp/dual_34g_a.log /tmp/dual_34g_b.log || true

wait $PA; RA=$?
wait $PB; RB=$?

echo "=== child logs (resample) ==="
grep RESAMPLE /tmp/dual_34g_a.log /tmp/dual_34g_b.log || true
echo "exit left=$RA right=$RB"
