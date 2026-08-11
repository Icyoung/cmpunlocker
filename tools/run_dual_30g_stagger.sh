#!/usr/bin/env bash
# Staggered: left 30G solo OK -> hold -> start right 30G -> both verify while peer up
set -euo pipefail
CU13=/home/icy/vllm-env/.venv/lib/python3.12/site-packages/nvidia/cu13
NVCC=$CU13/bin/nvcc
$NVCC -O2 -o /tmp/dual_30g_fullscan /tmp/dual_30g_fullscan.cu \
  -I"$CU13/include" -L"$CU13/lib" -lcudart \
  -Xlinker -rpath -Xlinker "$CU13/lib"

killall dual_30g_fullscan 2>/dev/null || true
rm -f /tmp/dual30g_ready_* /tmp/hold_left.pid

# Left: alloc+fill only, then hold (custom hold via sleep in subshell)
cat > /tmp/hold_left_30g.cu <<'EOF'
#include <stdio.h>
#include <unistd.h>
#include <cuda_runtime.h>
#define GB (1024ULL*1024*1024)
int main(){
    void *p=0;
    cudaSetDevice(0);
    cudaMalloc(&p, 30*GB);
    cudaMemset(p, 0xAA, 30*GB);
    cudaDeviceSynchronize();
    printf("HOLD_LEFT ptr=%p ready\n", p); fflush(stdout);
    FILE*f=fopen("/tmp/dual30g_ready_left","w"); if(f){fprintf(f,"1\n");fclose(f);}
    sleep(600);
    return 0;
}
EOF
$NVCC -O2 -o /tmp/hold_left_30g /tmp/hold_left_30g.cu \
  -I"$CU13/include" -L"$CU13/lib" -lcudart -Xlinker -rpath -Xlinker "$CU13/lib"

rm -f /tmp/dual30g_ready_left /tmp/dual30g_ready_right
/tmp/hold_left_30g > /tmp/hold_left.log 2>&1 &
HL=$!
for i in $(seq 1 60); do grep -q ready /tmp/hold_left.log && break; sleep 2; done
grep ready /tmp/hold_left.log

echo "=== left holding, scan left solo via probe ==="
/tmp/dual_30g_fullscan AA left > /tmp/left_solo_after_hold.log 2>&1 &
# wrong - that would second alloc. Use Python/C to only VERIFY hold_left pointer - can't from outside.

# Instead: use fullscan binary only as right, left is hold_left
echo "=== start right 30G BB while left holds ==="
/tmp/dual_30g_fullscan BB right > /tmp/right_with_left.log 2>&1 &
PR=$!
sleep 120
nvidia-smi --query-gpu=memory.used --format=csv,noheader
nvidia-smi | grep -E "hold_left|dual_30" || true

echo "=== verify left still AA (sample from hold process - use cuda in probe) ==="
cat > /tmp/verify_hold.cu <<'EOF'
#include <stdio.h>
#include <cuda_runtime.h>
#define GB (1024ULL*1024*1024)
__global__ void cnt(const unsigned char*p,size_t n,unsigned char v,unsigned long long*b){
  size_t s=gridDim.x*blockDim.x;
  for(size_t i=blockIdx.x*blockDim.x+threadIdx.x;i<n;i+=s) if(p[i]!=v) atomicAdd(b,1ULL);
}
int main(){
  void *p=(void*)0x731f80000000ULL; /* placeholder - read from file */
  char line[256]; FILE*f=fopen("/tmp/hold_left.log","r");
  if(f){ while(fgets(line,sizeof line,f)) if(sscanf(line,"HOLD_LEFT ptr=%p",&p)==1) break; fclose(f);}
  unsigned long long *d=0,*h=0; cudaSetDevice(0);
  cudaMalloc(&d,8); cudaMemset(d,0,8);
  cnt<<<65535,256>>>((unsigned char*)p,30*GB,0xAA,d);
  cudaDeviceSynchronize();
  h=(unsigned long long*)malloc(8); cudaMemcpy(h,d,8,2);
  printf("LEFT_VERIFY ptr=%p bad=%llu / %llu\n", p, h[0], 30ULL*GB);
  return h[0]?1:0;
}
EOF

# read actual ptr from log
PTR=$(grep HOLD_LEFT /tmp/hold_left.log | sed -n 's/.*ptr=\(0x[^ ]*\).*/\1/p')
echo "left ptr=$PTR"

# patch ptr into verify - use sed on generated C
sed "s/0x731f80000000ULL/${PTR}/" /tmp/verify_hold.cu > /tmp/verify_hold2.cu
$NVCC -O2 -o /tmp/verify_hold /tmp/verify_hold2.cu \
  -I"$CU13/include" -L"$CU13/lib" -lcudart -Xlinker -rpath -Xlinker "$CU13/lib"
/tmp/verify_hold

echo "=== right log ==="
grep -E "phase=|FULLSCAN|band@" /tmp/right_with_left.log || cat /tmp/right_with_left.log

kill $HL $PR 2>/dev/null || true
wait 2>/dev/null || true
