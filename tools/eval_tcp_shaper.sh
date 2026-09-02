#!/usr/bin/env bash
# Unified TCP-shaper eval: 4 metrics, printed as one comparison line.
#   M1 cap goodput   : iperf3 @8G cap, no RR         -> shaping accuracy
#   M2 idle latency  : TCP_RR, no background         -> small-pkt lat when idle
#   M3 sat  latency  : TCP_RR while iperf3 fills 8G  -> the key metric
#   M4 sat  goodput  : iperf3 goodput during M3      -> throughput not sacrificed
# Assumes EDT already applied on dpu1vf0 (8G cap vf0->vf0) and RR server on 5353.
set -u
export PATH=/usr/sbin:$PATH
PT=/home/zhaoxiang/hyperfront/perftest-enhanced/ib_write_bw   # unused
RR=/home/zhaoxiang/hyperfront/hpft-implementation/tools/tcp_rr_client.py
LABEL="${1:-variant}"
DST=10.1.0.2

rr() { python3 $RR $DST 5353 "$1" 2>/dev/null | grep -oE "p50=[0-9.]+ p90=[0-9.]+ p99=[0-9.]+" ; }
gp() { grep receiver /tmp/$1 2>/dev/null | awk '{print $(NF-2)}'; }

ssh -o BatchMode=yes "$RECV" 'ss -ltn | grep -q 5353 || (setsid python3 /tmp/tcp_rr_server.py 5353 >/dev/null 2>&1 & sleep 0.5)' 2>/dev/null

# M1: cap goodput (no RR)
ssh -o BatchMode=yes "$RECV" 'pkill -u zhaoxiang iperf3 2>/dev/null; sleep 0.5; setsid nohup iperf3 -s -1 -p 5381 >/tmp/ips.log 2>&1 </dev/null & sleep 1'
timeout 14 iperf3 -c $DST -p 5381 -t 7 -f g >/tmp/m1.log 2>&1; M1=$(gp m1.log)

# M2: idle latency
M2=$(rr 3000)

# M3+M4: saturated latency + goodput (concurrent)
ssh -o BatchMode=yes "$RECV" 'pkill -u zhaoxiang iperf3 2>/dev/null; sleep 0.5; setsid nohup iperf3 -s -1 -p 5382 >/tmp/ips.log 2>&1 </dev/null & sleep 1'
( timeout 18 iperf3 -c $DST -p 5382 -t 12 -f g >/tmp/m4.log 2>&1 & ); sleep 3
M3=$(rr 3000)
# wait for iperf3 to finish (t=12) before reading its final goodput
# The receiver follows the registry: these micro-benchmarks are two-node by
# nature, but which two depends on the run's role assignment.
RECV=${RECV:-$(python3 -c "import json;print(json.load(open('/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-registry.json'))['receiver_host'])")}
for _ in $(seq 1 15); do grep -q receiver /tmp/m4.log 2>/dev/null && break; sleep 1; done
M4=$(gp m4.log)
ssh -o BatchMode=yes "$RECV" 'pkill -u zhaoxiang iperf3 2>/dev/null'

printf "%-22s | M1 cap-gp=%-6s | M2 idle-lat[%s] | M3 SAT-lat[%s] | M4 sat-gp=%s\n" \
  "$LABEL" "${M1:-?}G" "${M2:-?}" "${M3:-?}" "${M4:-?}G"
