#!/usr/bin/env bash
# TCP rate-change efficiency vs frequency: for each wave freq, toggle 8G/2G
# (avg cap 5G) during an 8s flow, report iperf3 goodput vs the 4.99G static-5G
# reference. Goodput drop = cost of the data-plane transient per rate change.
set -u
export PATH=/usr/sbin:$PATH
cd /home/zhaoxiang/hyperfront/hpft-exp-deprecated/tcp_shaper
REG=/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-tcp-registry.json
SRC="sgpu01/0000:38:00.1/vf0"; DST="sgpu02/0000:38:00.1/vf0"; PIN=/sys/fs/bpf/hpft_tcp_edt
set_rate() { sudo -n env PATH=/usr/sbin:$PATH tools/tcp-shaper-controller \
    --registry $REG --src-vnic "$SRC" --dst-vnic "$DST" --rate-bps "$1" \
    --pin-dir $PIN --apply >/dev/null 2>&1; }

for WHZ in "$@"; do
  half=$(python3 -c "print(1/(2*$WHZ))")
  set_rate 8000000000
  ssh -o BatchMode=yes sgpu02 "pkill -u zhaoxiang iperf3 2>/dev/null; setsid nohup iperf3 -s -1 -p 5320 >/tmp/ips.log 2>&1 </dev/null & sleep 1"
  ( timeout 16 iperf3 -c 10.1.0.2 -p 5320 -t 10 -f g >/tmp/we_$WHZ.log 2>&1 & )
  sleep 2
  t_end=$(( $(date +%s%N) + 8000000000 )); hi=0
  while [ $(date +%s%N) -lt $t_end ]; do
    if [ $hi -eq 0 ]; then set_rate 2000000000; hi=1; else set_rate 8000000000; hi=0; fi
    python3 -c "import time;time.sleep($half)"
  done
  set_rate 8000000000
  wait 2>/dev/null
  gp=$(grep receiver /tmp/we_$WHZ.log | awk '{print $(NF-2)}')
  python3 -c "g=float('$gp' or 0); print(f'wave={$WHZ:>4}Hz  goodput={g:.2f}G  loss_vs_5G={100*(4.99-g)/4.99:+.0f}%%  updates/s={2*$WHZ}')"
done
