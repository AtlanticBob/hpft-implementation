#!/usr/bin/env bash
# TCP data-plane step response: active flow, sample host VF tx rate at 10ms,
# step the cap mid-run and measure settle time.
set -u
export PATH=/usr/sbin:$PATH
cd /home/zhaoxiang/hyperfront/hpft-exp/tcp_shaper
REG=/home/zhaoxiang/hyperfront/hpft-v2/config/lab-tcp-registry.json
SRC="sgpu01/0000:38:00.1/vf0"; DST="sgpu02/0000:38:00.1/vf0"; PIN=/sys/fs/bpf/hpft_tcp_edt
DEV=dpu1vf0
CNT=/sys/class/net/$DEV/statistics/tx_bytes
SAMPLES=/tmp/tcp_step_samples.csv

set_rate() { sudo -n env PATH=/usr/sbin:$PATH tools/tcp-shaper-controller \
    --registry $REG --src-vnic "$SRC" --dst-vnic "$DST" \
    --rate-bps "$1" --pin-dir $PIN --apply >/dev/null 2>&1; }

# start server + flow
ssh -o BatchMode=yes sgpu02 'pkill -u zhaoxiang iperf3 2>/dev/null; setsid nohup iperf3 -s -1 -p 5310 >/tmp/ips.log 2>&1 </dev/null & sleep 1'
set_rate 8000000000
( timeout 20 iperf3 -c 10.1.0.2 -p 5310 -t 16 >/dev/null 2>&1 & )
sleep 3

# sample tx_bytes at 10ms for 8s; step 8G->2G at ~t=3s into sampling
echo "ts_ns,gbps,phase" > $SAMPLES
prev=$(cat $CNT); pt=$(date +%s%N); step_done=0; t0=$(date +%s%N)
for i in $(seq 1 800); do
  now=$(date +%s%N)
  if [ $step_done -eq 0 ] && [ $(( (now - t0) / 1000000 )) -ge 3000 ]; then
    step_ns_before=$(date +%s%N); set_rate 2000000000; step_ns=$(date +%s%N); step_done=1
    echo "# STEP 8G->2G issued at ts=$step_ns (call took $(( (step_ns-step_ns_before)/1000000 ))ms)" >> $SAMPLES
  fi
  v=$(cat $CNT); dt=$(( now - pt ))
  if [ $dt -gt 0 ]; then g=$(python3 -c "print(f'{($v-$prev)*8/$dt:.3f}')"); ph=$([ $step_done -eq 1 ] && echo after || echo before); echo "$now,$g,$ph" >> $SAMPLES; fi
  prev=$v; pt=$now
  python3 -c "import time; time.sleep(0.01)"
done
wait 2>/dev/null

# analyze settle time
python3 - "$SAMPLES" <<'PY'
import sys, csv
rows=[]; step=None
for line in open(sys.argv[1]):
    if line.startswith("# STEP"): step=int(line.split("ts=")[1].split()[0]); continue
    if line.startswith("ts_ns"): continue
    p=line.strip().split(","); rows.append((int(p[0]), float(p[1]), p[2]))
before=[g for t,g,ph in rows if ph=="before"][-30:]
after=[(t,g) for t,g,ph in rows if ph=="after"]
import statistics
b=statistics.mean(before) if before else 0
tgt=2.0
print(f"before-step rate: {b:.2f} Gbps (cap 8G)")
# settle = first time after step where 5 consecutive samples within +-15% of 2G
settle=None
for i in range(len(after)-5):
    win=[g for _,g in after[i:i+5]]
    if all(abs(g-tgt)/tgt < 0.15 for g in win):
        settle=(after[i][0]-step)/1e6; break
tail=[g for _,g in after[-30:]]
print(f"after-step tail rate: {statistics.mean(tail):.2f} Gbps (cap 2G)")
print(f"settle time (8G->2G, within +-15%% of 2G): {settle:.0f} ms" if settle else "did not settle in window")
PY
