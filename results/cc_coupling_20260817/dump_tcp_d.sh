#!/usr/bin/env bash
# Sample the TCP shaper's observer state while a run is in flight.
# usage: dump_tcp_d.sh <tag> <seconds>
B=${HPFT_BPFTOOL:-/usr/lib/linux-tools-5.15.0-185/bpftool}
OUT="$(cd "$(dirname "$0")" && pwd)/results/$1_tcpd.txt"
: > "$OUT"
for i in $(seq 1 "$2"); do
  echo "t=$(date +%s.%N)" >> "$OUT"
  sudo $B map dump pinned /sys/fs/bpf/hpft_tcp_edt/maps/hpft_pair_state 2>/dev/null \
    | python3 -c "
import sys, json
try: rows = json.load(sys.stdin)
except Exception: sys.exit()
for r in rows:
    v = r['value']
    if v.get('cc_sum'):
        print('  pair=%x cc_sum=%d cc_prev=%d d=%d cuts=%d'
              % (r['key'], v['cc_sum'], v['cc_prev'], v['d'], v['cuts']))
" >> "$OUT"
  sleep 2
done
