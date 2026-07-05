#!/usr/bin/env bash
# TCP shaper control-path frequency scan: for each target Hz, run N direct-bpf
# updates and report achieved Hz + per-update latency percentiles.
set -u
export PATH=/usr/sbin:$PATH
cd /home/zhaoxiang/hyperfront/hpft-exp/tcp_shaper
REG=/home/zhaoxiang/hyperfront/hpft-shaper-v2/config/lab-tcp-registry.json
SRC="sgpu01/0000:38:00.1/vf0"; DST="sgpu02/0000:38:00.1/vf0"; PIN=/sys/fs/bpf/hpft_tcp_edt
OUTDIR=/tmp/tcp_freq_scan; mkdir -p $OUTDIR
for HZ in "$@"; do
  N=$((HZ * 2)); [ $N -gt 20000 ] && N=20000
  sudo -n env PATH=/usr/sbin:$PATH tools/tcp-shaper-controller \
    --registry $REG --src-vnic "$SRC" --dst-vnic "$DST" \
    --rates-bps 8000000000,4000000000 \
    --loop-count $N --loop-hz $HZ --pin-dir $PIN --apply --json \
    > $OUTDIR/hz_$HZ.json 2>$OUTDIR/hz_$HZ.err
  python3 - "$HZ" "$OUTDIR/hz_$HZ.json" <<'PY'
import sys, json
hz = sys.argv[1]
try:
    d = json.load(open(sys.argv[2]))
    s = d["summary"]; lat = s["direct_update_latency_ms"]
    ah = s["achieved_hz"] or 0.0
    print("target=%6sHz  achieved=%9.1fHz  completed=%d/%d  misses=%d  "
          "updlat p50=%.4fms p99=%.4fms max=%.3fms"
          % (hz, ah, s["completed"], s["attempted"],
             s["deadline_misses_over_one_interval"],
             lat["p50"] or 0, lat["p99"] or 0, lat["max"] or 0))
except Exception as e:
    print("target=%sHz  ERROR: %s" % (hz, str(e)[:80]))
PY
done
