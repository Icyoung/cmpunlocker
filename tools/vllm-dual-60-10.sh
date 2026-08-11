#!/usr/bin/env bash
# Two vLLM instances on one GPU: ~60G (27B) + ~10G (1.5B) for screenshot / 32G-wall demo.
set -euo pipefail

VLLM="${VLLM:-/home/icy/vllm-env/.venv/bin/vllm}"
BIG_MODEL="${BIG_MODEL:-/home/icy/Models/Qwen3.6-27B-official}"
SMALL_MODEL="${SMALL_MODEL:-/home/icy/Models/Qwen2.5-1.5B-Instruct}"
BIG_UTIL="${BIG_UTIL:-0.73}"
SMALL_UTIL="${SMALL_UTIL:-0.12}"
BIG_MAX_LEN="${BIG_MAX_LEN:-512}"
SMALL_MAX_LEN="${SMALL_MAX_LEN:-32768}"
LOG60="${LOG60:-/tmp/vllm60.log}"
LOG10="${LOG10:-/tmp/vllm10.log}"
READY="${READY:-/tmp/vllm-dual-ready}"

pkill -x vllm 2>/dev/null || true
sleep 2
rm -f "$READY" "$LOG60" "$LOG10"

# Launch together so each instance's utilization cap applies before either monopolizes VRAM.
nohup "$VLLM" serve "$BIG_MODEL" \
  --host 127.0.0.1 --port 8000 \
  --dtype bfloat16 --tensor-parallel-size 1 \
  --gpu-memory-utilization "$BIG_UTIL" \
  --max-model-len "$BIG_MAX_LEN" \
  --language-model-only --reasoning-parser qwen3 \
  --enforce-eager \
  >"$LOG60" 2>&1 &
pid60=$!

nohup "$VLLM" serve "$SMALL_MODEL" \
  --host 127.0.0.1 --port 8001 \
  --dtype bfloat16 --tensor-parallel-size 1 \
  --gpu-memory-utilization "$SMALL_UTIL" \
  --max-model-len "$SMALL_MAX_LEN" \
  --enforce-eager \
  >"$LOG10" 2>&1 &
pid10=$!

echo "vLLM60 pid=$pid60 util=$BIG_UTIL max_len=$BIG_MAX_LEN log=$LOG60"
echo "vLLM10 pid=$pid10 util=$SMALL_UTIL max_len=$SMALL_MAX_LEN log=$LOG10"

for _ in $(seq 1 120); do
  mem=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' ')
  h60=$(curl -sf -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/health 2>/dev/null || echo 0)
  h10=$(curl -sf -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/health 2>/dev/null || echo 0)
  ts=$(date +%H:%M:%S)
  echo "$ts mem=${mem}MiB health60=$h60 health10=$h10"

  if [ "$h60" = "200" ] && [ "$h10" = "200" ]; then
    date >"$READY"
    nvidia-smi
    echo "READY: both vLLM instances healthy ($READY)"
    exit 0
  fi

  if ! pgrep -f "vllm serve.*8000" >/dev/null; then
    echo "vLLM60 died:"
    tail -20 "$LOG60"
    exit 1
  fi
  if ! pgrep -f "vllm serve.*8001" >/dev/null; then
    echo "vLLM10 died:"
    tail -20 "$LOG10"
    exit 1
  fi
  sleep 10
done

echo "timeout"
nvidia-smi
exit 1
