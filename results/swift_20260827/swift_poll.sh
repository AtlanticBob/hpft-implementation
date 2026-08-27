#!/usr/bin/env bash
# runs ON a sender DPU: swift_poll.sh <dur_s> <interval_s> <out>
# Polls 0xdee for pairs 0..7 and copies the RSP/SET lines the RP prints
# (HPFT_SET carries the wall-clock t0 and the pair index of each answer).
DUR=$1; ITV=$2; OUT=$3
n=$(wc -l < /tmp/pcc_rp.log)
rm -f "$OUT"
end=$(python3 -c "import time;print(time.time()+$DUR)")
while python3 -c "import time,sys;sys.exit(0 if time.time()<$end else 1)"; do
  for i in 0 1 2 3 4 5 6 7; do echo "0xdee $i" > /tmp/rp_fifo; done
  sleep "$ITV"
done
sleep 0.5
tail -n +$((n+1)) /tmp/pcc_rp.log | grep -a "HPFT_RSP\|HPFT_SET ft=0xdee" > "$OUT"
