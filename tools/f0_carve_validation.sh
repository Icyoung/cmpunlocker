#!/bin/bash
# f0_carve_validation.sh — post-carve battery for 10GB CMP @ 80G.
# Progress goes to ~/f0/carve_progress.log (fsync'd per step) so a wedge
# still tells us the last passing stage.
set -u
LOG=~/f0/carve_progress.log
: > "$LOG"
step() { echo "$(date +%T) $*" | tee -a "$LOG"; sync; }

cd ~/f0

step "S0 health gate"
./f0_probe 2>&1 | tail -1 | tee -a "$LOG"; sync

step "S1 carve print + region map"
sudo -n dmesg 2>/dev/null | grep -E "CMP_CARVE|CMP_MEM_GSP_REGION" | tee -a "$LOG" || \
  dmesg 2>/dev/null | grep -E "CMP_CARVE|CMP_MEM_GSP_REGION" | tee -a "$LOG"
sync

step "S2 warmup sm_vs_ce"
timeout 120 ./f0_sm_vs_ce 2>&1 | tail -2 | tee -a "$LOG"; sync

step "S3 full-range drip 1G chunks [0,60G)"
timeout 260 env CHUNK_MB=1024 ./f0_slow_drip 0 60 >> "$LOG" 2>&1
step "S3 drip exit=$? (see drip_progress.log for last chunk)"

step "S4 60G single-launch write in 60G alloc (former killer)"
timeout 120 env ALLOC_GB=60 WATCH_SECS=20 ./f0_size_probe 60 60 1 >> "$LOG" 2>&1
step "S4 exit=$?"

step "S5 fake_sync_check TARGET_GB=40 (the decisive 70G prefill)"
timeout 240 env TARGET_GB=40 ./f0_fake_sync_check >> "$LOG" 2>&1
step "S5 exit=$?"

step "S6 torture 70G x3"
timeout 240 env ROUNDS=3 CAP_GB=70 ./f0_torture >> "$LOG" 2>&1
step "S6 exit=$?"

step "S7 verify x2"
./f0_verify 2>&1 | tail -1 | tee -a "$LOG"
./f0_verify 2>&1 | tail -1 | tee -a "$LOG"
sync

step "ALL STAGES DONE"
