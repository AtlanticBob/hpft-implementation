#!/usr/bin/env bash
# L2 churn on the L1b policy-bottleneck baseline. FIX vs first attempt:
# ib_write_bw -D must match between client and server (ops_notes), so each
# RDMA (re)start spawns its OWN matching-duration server on a unique port.
# Timeline (180s):
#   t=0    all 4 pairs up (RDMA+TCP)
#   t=45   TENANT churn: vf1 pair exits (D=45 expiry)
#   t=75   vf1 rejoins (D=105)
#   t=105  CLASS churn: kill vf0 TCP -> vf0 RDMA should fill vf0's 40G cap
#   t=135  vf0 TCP rejoins
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
mark() { echo "$(date +%s.%N) $1" >> "$DIR/l2_events.txt"; }
PORT=24030

rdma() { # n dur   (own matching-D server on a fresh port)
    local n=$1 d=$2 p=$PORT; PORT=$((PORT+1))
    ssh -f sgpu02 "$PT -d mlx5_$((6+n)) -p $p --report_gbits -D $d > /tmp/l2_rsrv.log 2>&1"
    sleep 1
    $PT -d mlx5_$((6+n)) -p $p --report_gbits -D "$d" "10.1.$n.2" \
        > "$DIR/l2_rdma${n}_$(date +%s).log" 2>&1 &
}
tcp() { # n dur   (strong SO_BINDTODEVICE bind per user's hint)
    iperf3 -B "10.1.$1.1%dpu1vf$1" -c "10.1.$1.2" -p $((5201+4*$1)) -P 4 -b 0 -t "$2" -J \
        > "$DIR/l2_tcp${1}_$(date +%s).json" 2>&1 &
}

cd "$REPO"
ssh hpft-dpu2 "ethtool p1 | grep Speed"
# Do NOT restart any agent: restarting RP+tx left class attribution in a
# transient (tcp folded into rdma, nfs=4) that outlasted the analysis
# window. Run churn on the warm, confirmed-working agents. Guard: a brief
# 4-pair MIXED probe must yield nfs>=8 (both classes detected) before the
# timeline.
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
for n in 0 1 2 3; do ssh -f sgpu02 "$PT -d mlx5_$((6+n)) -p $((24070+n)) --report_gbits -D 6 > /tmp/g_s$n.log 2>&1"; done
sleep 1
for n in 0 1 2 3; do $PT -d mlx5_$((6+n)) -p $((24070+n)) --report_gbits -D 6 10.1.$n.2 >/dev/null 2>&1 & iperf3 -B 10.1.$n.1 -c 10.1.$n.2 -p $((5201+4*n)) -P4 -b 0 -t 6 >/dev/null 2>&1 & done
sleep 4
gnfs=$(ssh hpft-dpu2 'tail -1 /tmp/hpft_rxagent_e.jsonl' | python3 -c "import json,sys; print(json.load(sys.stdin)['nfs'])")
echo "attribution guard: nfs=$gnfs (need >=8)"
wait 2>/dev/null
[ "$gnfs" -ge 8 ] || { echo "GUARD FAIL: nfs=$gnfs, class attribution not both-class, aborting"; exit 1; }
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
sleep 2

rm -f "$DIR/l2_events.txt"
t0=$(date +%s.%N); echo "$t0" > "$DIR/l2_t0.txt"; mark start

rdma 0 180; tcp 0 180
rdma 2 180; tcp 2 180
rdma 3 180; tcp 3 180
rdma 1 45;  tcp 1 45          # vf1 leaves at t=45
sleep 46
mark kill_vf1_tenant
sleep 29
mark rejoin_vf1
rdma 1 103; tcp 1 103          # vf1 back to end
sleep 30
mark kill_vf0_tcp
pkill -f "iperf3 -[B] 10.1.0.1"   # drop vf0 TCP only (unique bind, [B] guards self-match)
sleep 30
mark rejoin_vf0_tcp
tcp 0 45
sleep 45
mark end
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/l2_rx.jsonl"
echo l2-done
