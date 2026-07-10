#!/usr/bin/env bash
# Soak watchdog (cron */5): liveness + log freshness of the E stack.
LOG=/home/zhaoxiang/hyperfront/hpft-shaper-v2/results/soak_20260710/watchdog.log
TS=$(date -u +%FT%TZ)
RX=$(ssh -o BatchMode=yes -o ConnectTimeout=8 hpft-dpu2 \
  'echo -n "$(systemctl is-active hpft-rxagent-e) "; date -r /tmp/hpft_rxagent_e.jsonl +%s 2>/dev/null || echo 0' 2>&1)
TX=$(ssh -o BatchMode=yes -o ConnectTimeout=8 hpft-dpu \
  'echo -n "$(systemctl is-active hpft-txagent-e) "; echo -n "$(date -r /tmp/hpft_txagent_e.jsonl +%s 2>/dev/null || echo 0) "; pgrep -xc doca_pcc' 2>&1)
SHIM=$(systemctl is-active hpft-pace-shim 2>/dev/null)
echo "$TS now=$(date +%s) rx=[$RX] tx=[$TX] shim=$SHIM" >> $LOG
