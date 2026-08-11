#!/usr/bin/env bash
set -euo pipefail
CU13=/home/icy/vllm-env/.venv/lib/python3.12/site-packages/nvidia/cu13
rm -f /tmp/dual34g_ready /tmp/dual34g_ready_left /tmp/dual34g_ready_right
rm -f /tmp/dual34g_left.log /tmp/dual34g_right.log

cp /tmp/dual_34g_fullscan.c /tmp/dual_34g_fullscan.cu
NVCC=/home/icy/vllm-env/.venv/lib/python3.12/site-packages/nvidia/cu13/bin/nvcc
$NVCC -O2 -o /tmp/dual_34g_fullscan /tmp/dual_34g_fullscan.cu \
  -I"$CU13/include" -L"$CU13/lib" -lcudart \
  -Xlinker -rpath -Xlinker "$CU13/lib"

/tmp/dual_34g_fullscan AA left  > /tmp/dual34g_left.log 2>&1 &
PL=$!
/tmp/dual_34g_fullscan BB right > /tmp/dual34g_right.log 2>&1 &
PR=$!

for i in $(seq 1 180); do
  if grep -q "PEER-UP scan" /tmp/dual34g_left.log && grep -q "PEER-UP scan" /tmp/dual34g_right.log; then
  break
  fi
  sleep 2
done

echo "=== nvidia-smi ==="
nvidia-smi --query-gpu=memory.used --format=csv,noheader
nvidia-smi | grep dual_34g || true

echo "=== LEFT ==="
cat /tmp/dual34g_left.log
echo "=== RIGHT ==="
cat /tmp/dual34g_right.log

wait $PL; RL=$?
wait $PR; RR=$?
echo "exit left=$RL right=$RR"
exit $(( RL || RR ))
