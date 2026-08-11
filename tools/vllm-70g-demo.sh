#!/usr/bin/env bash
# Launch vLLM Qwen bf16 and signal when GPU mem crosses SCREENSHOT_MIB.
set -euo pipefail

MODEL="${MODEL:-/home/icy/Models/Qwen3.6-27B-official}"
VLLM="${VLLM:-/home/icy/vllm-env/.venv/bin/vllm}"
PORT="${PORT:-8000}"
GPU_UTIL="${GPU_UTIL:-0.88}"
MAX_LEN="${MAX_LEN:-57344}"
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"
HOLD_GB="${HOLD_GB:-10}"
HOLD_BIN="${HOLD_BIN:-/home/icy/f0/hold_test}"
SCREENSHOT_MIB="${SCREENSHOT_MIB:-67000}"
LOG="${LOG:-/tmp/vllm-70g.log}"
READY="${READY:-/tmp/vllm-screenshot-ready}"

pkill -f "vllm serve" 2>/dev/null || true
sleep 2
rm -f "$READY" "$LOG"

# Optional FLR if GPU stuck from prior crash
if [ "${FLR:-0}" = "1" ]; then
  PCI=$(lspci | awk '/NVIDIA/{print $1; exit}')
  sudo modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia 2>/dev/null || true
  sleep 2
  echo 1 | sudo tee "/sys/bus/pci/devices/0000:${PCI}/reset" >/dev/null
  sleep 3
  sudo modprobe nvidia
  sudo modprobe nvidia_modeset
  sleep 2
fi

EAGER_ARGS=()
[ "$ENFORCE_EAGER" = "1" ] && EAGER_ARGS+=(--enforce-eager)

nohup "$VLLM" serve "$MODEL" \
  --host 127.0.0.1 \
  --port "$PORT" \
  --dtype bfloat16 \
  --tensor-parallel-size 1 \
  --gpu-memory-utilization "$GPU_UTIL" \
  --max-model-len "$MAX_LEN" \
  --language-model-only \
  --reasoning-parser qwen3 \
  "${EAGER_ARGS[@]}" \
  >"$LOG" 2>&1 &

echo "vLLM pid=$! max-model-len=$MAX_LEN gpu-util=$GPU_UTIL log=$LOG"
echo "Will add hold_test ${HOLD_GB}G when vLLM mem >= 50000 MiB"
echo "Watching total >= ${SCREENSHOT_MIB} MiB -> $READY"

hold_started=0
for _ in $(seq 1 240); do
  mem=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' ')
  ts=$(date +%H:%M:%S)

  if [ "$hold_started" = "0" ] && [ "$mem" -ge 50000 ] 2>/dev/null && [ -x "$HOLD_BIN" ]; then
    nohup "$HOLD_BIN" "$HOLD_GB" 7200 >/tmp/hold_test.log 2>&1 &
    hold_started=1
    echo "$ts hold_test ${HOLD_GB}G pid=$!"
    sleep 3
    mem=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | tr -d ' ')
  fi

  if [ "$mem" -ge "$SCREENSHOT_MIB" ] 2>/dev/null; then
    date >"$READY"
    nvidia-smi
    echo "READY: total ${mem}MiB >= ${SCREENSHOT_MIB} — screenshot now ($READY)"
    exit 0
  fi

  if curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    echo "$ts mem=${mem}MiB HEALTHY (vLLM up; hold=${hold_started})"
  else
    echo "$ts mem=${mem}MiB loading... (hold=${hold_started})"
    if ! pgrep -f "vllm serve" >/dev/null; then
      if [ "$hold_started" = "1" ] && [ "$mem" -ge "$SCREENSHOT_MIB" ] 2>/dev/null; then
        date >"$READY"
        nvidia-smi
        echo "READY: vLLM died but mem still high — screenshot now ($READY)"
        exit 0
      fi
      echo "vLLM exited; tail log:"
      tail -25 "$LOG"
      exit 1
    fi
  fi
  sleep 5
done

echo "timeout; last nvidia-smi:"
nvidia-smi
exit 1
