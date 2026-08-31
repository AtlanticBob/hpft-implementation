#!/usr/bin/env bash
# Sample the TCP executor's trust per pair on this host during a run.
# usage: bpf_trust_sample.sh <duration_s> <out_jsonl> [interval_s=0.1]
# record: {"ts", "key", "trust", "loss_ep", "loss_age_ms", "cc_sum"} for pairs
# with traffic. loss_ep counts the epochs loss evidence raised the trust;
# loss_age_ms is how long ago this flow-set last retransmitted (-1 = never).
DUR=$1; OUT=$2; ITV=${3:-0.1}
B=$(ls -1 /usr/lib/linux-tools-*/bpftool 2>/dev/null | sort -V | tail -1); B=${B:-$(command -v bpftool)}
END=$(python3 -c "import time;print(time.time()+$DUR)")
: > "$OUT"
while python3 -c "import time,sys;sys.exit(0 if time.time()<$END else 1)"; do
  sudo "$B" map dump pinned /sys/fs/bpf/hpft_tcp_edt/maps/hpft_pair_state -j 2>/dev/null | python3 -c "
import json,sys,struct,time
t=time.time()
try: d=json.load(sys.stdin)
except Exception: d=[]
for e in d:
    v=bytes(int(x,16) for x in e['value']); k=bytes(int(x,16) for x in e['key']); f=struct.unpack('<IIQQQQQIIIIQQ',v[:80])
    if not f[2]: continue
    age=-1 if not f[12] else (time.monotonic_ns()-f[12])//1000000
    print(json.dumps({'ts':t,'key':k.hex(),'trust':round(f[9]/65536,4),'loss_ep':f[10],'loss_age_ms':age,'cc_sum':f[4]}))" >> "$OUT"
  sleep "$ITV"
done
