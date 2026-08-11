#!/usr/bin/env bash
set -euo pipefail
CU13=/home/icy/vllm-env/.venv/lib/python3.12/site-packages/nvidia/cu13
NVCC=$CU13/bin/nvcc
rm -f /tmp/dual30g_ready_* /tmp/dual30g_left.log /tmp/dual30g_right.log

$NVCC -O2 -o /tmp/dual_30g_fullscan /tmp/dual_30g_fullscan.cu \
  -I"$CU13/include" -L"$CU13/lib" -lcudart \
  -Xlinker -rpath -Xlinker "$CU13/lib"

killall dual_30g_fullscan 2>/dev/null || true
sleep 1

echo "=== dual 30G+30G simultaneous (AA left, BB right) ==="
/tmp/dual_30g_fullscan AA left  > /tmp/dual30g_left.log 2>&1 &
PL=$!
/tmp/dual_30g_fullscan BB right > /tmp/dual30g_right.log 2>&1 &
PR=$!

for i in $(seq 1 180); do
  if grep -q "PEER-UP-AFTER-TOUCH" /tmp/dual30g_left.log && \
     grep -q "PEER-UP-AFTER-TOUCH" /tmp/dual30g_right.log; then
    break
  fi
  sleep 3
done

echo "=== nvidia-smi ==="
nvidia-smi --query-gpu=memory.used --format=csv,noheader
echo "=== LEFT ==="
cat /tmp/dual30g_left.log
echo "=== RIGHT ==="
cat /tmp/dual30g_right.log

wait $PL; RL=$?
wait $PR; RR=$?
echo "exit left=$RL right=$RR"
exit $(( RL || RR ))
