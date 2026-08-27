#!/usr/bin/env bash
# run.sh <tag> <scenario> [algo-note]   -> results/<tag>/
# scenarios: solo | incast4 | port6 | tcpmix
# Receiver = sgpu02 (vport meter on hpft-dpu2). RDMA via perftest --start_at
# (both ends), -x 3 -m 1024. Telemetry: 0xdee poll on each sender DPU (250 ms)
# + receiver vport meter (100 ms).
set -u
TAG=$1; SC=$2
BASE=$(cd "$(dirname "$0")" && pwd)
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
DIR=$BASE/results/$TAG; mkdir -p "$DIR"
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
RECV=sgpu02; RDPU=hpft-dpu2; TCPCC=${TCPCC:-cubic}
# columns: src_host src_vf dst_vf type count start end
case $SC in
solo)    END=30; FLOWS="sgpu01 1 4 rdma 10 0 30" ;;
solo1q)  END=30; FLOWS="sgpu01 1 4 rdma 1 0 30" ;;
incast4) END=30; FLOWS="sgpu01 1 4 rdma 10 0 30
sgpu03 1 4 rdma 10 0 30
sgpu04 1 4 rdma 10 0 30
sgpu03 3 4 rdma 10 0 30" ;;
port6)   END=30; FLOWS="sgpu01 1 2 rdma 10 0 30
sgpu03 1 3 rdma 10 0 30
sgpu04 1 4 rdma 10 0 30
sgpu01 3 5 rdma 10 0 30
sgpu03 3 6 rdma 10 0 30
sgpu04 3 7 rdma 10 0 30" ;;
two1)    END=15; FLOWS="sgpu01 1 2 rdma 1 0 15
sgpu01 3 5 rdma 1 0 15" ;;
two10)   END=15; FLOWS="sgpu01 1 2 rdma 10 0 15
sgpu01 3 5 rdma 10 0 15" ;;
tcpmix)  END=30; FLOWS="sgpu01 0 0 tcp 10 10 20
sgpu03 0 1 tcp 10 10 20
sgpu04 0 2 tcp 10 10 20
sgpu04 1 3 tcp 10 10 20
sgpu01 1 4 rdma 10 0 30
sgpu03 1 5 rdma 10 0 30
sgpu01 2 6 rdma 10 0 30
sgpu03 2 7 rdma 10 0 30" ;;
*) echo "unknown scenario"; exit 2 ;;
esac
WARM=5
dev_of() { python3 -c "
import json;r=json.load(open('$REPO/config/lab-registry.json'))
print(next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }
ip_of() { python3 -c "
import json;r=json.load(open('$REPO/config/lab-registry.json'))
print(next(v['ip'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }
dpu_of() { case $1 in sgpu01) echo hpft-dpu;; sgpu02) echo hpft-dpu2;; sgpu03) echo hpft-dpu3;; sgpu04) echo hpft-dpu4;; esac; }
on_host() { if [ "$1" = "$(hostname)" ]; then shift; bash -c "$*"; else h=$1; shift; ssh -n -o BatchMode=yes "$h" "$*" </dev/null; fi; }
snap_cnp() {
  for h in sgpu01 sgpu03 sgpu04; do
    on_host "$h" 'for c in rp_cnp_handled np_cnp_sent; do printf "%s.mlx5_3.%s=%s\n" '"$h"' $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done'
  done
  ssh "$RECV" 'for c in np_cnp_sent np_ecn_marked_roce_packets; do printf "sgpu02.mlx5_3.%s=%s\n" $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done'
}
echo "$FLOWS" > "$DIR/flows.txt"; echo "$SC" > "$DIR/scenario.txt"; echo "${3:-}" > "$DIR/note.txt"
for h in $RECV sgpu01 sgpu03 sgpu04; do on_host "$h" 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; true'; done
sleep 1
# fresh pair table on the sender DPUs: delete every registered pair (autoreg
# re-creates them on the first event), then re-send the CC choice, which
# resets the per-pair CC state.
SENDERS=$(echo "$FLOWS" | awk '$4=="rdma"{print $1}' | sort -u)
ALGO=${ALGO:-3}
for h in $SENDERS; do d=$(dpu_of $h); ssh -n $d 'for i in $(seq 0 15); do n=$(wc -l < /tmp/pcc_rp.log); echo "0xdee $i" > /tmp/rp_fifo; sleep 0.06; ft=$(tail -n +$((n+1)) /tmp/pcc_rp.log | grep -a HPFT_RSP | head -1 | sed "s/.*ft=\(0x[0-9a-f]*\).*/\1/"); [ -n "$ft" ] && [ "$ft" != 0x0 ] && echo "$ft 0" > /tmp/rp_fifo; sleep 0.03; done; echo "0xccd '$ALGO'" > /tmp/rp_fifo; sleep 0.1; n=$(wc -l < /tmp/pcc_rp.log); echo "0xdee 0" > /tmp/rp_fifo; sleep 0.1; echo -n "'$d' pair0 after clear: "; tail -n +$((n+1)) /tmp/pcc_rp.log | grep -a HPFT_RSP | head -1 | cut -c1-40' </dev/null; done
T0=$(python3 -c "import time;print(int(time.time())+20)")
echo "$T0" > "$DIR/t0.txt"
k=0
while read -r sh sv dv ty n st en; do
  dur=$((en-st)); off=$((WARM+st)); [ "$st" -eq 0 ] && { dur=$((en+WARM)); off=0; }
  if [ "$ty" = rdma ]; then
    ssh -o BatchMode=yes -f "$RECV" "$PT -d $(dev_of $RECV $dv) -x 3 -m 1024 -q $n -p $((19300+k)) --report_gbits -D $dur --start_at=$((T0+off)) > /tmp/sw_s$k.log 2>&1"
  else
    ssh -o BatchMode=yes -f "$RECV" "iperf3 -s -1 -p $((5601+k)) >/tmp/sw_s$k.log 2>&1"
  fi
  k=$((k+1))
done <<<"$FLOWS"
sleep 2
snap_cnp > "$DIR/cnp_pre.txt"
timeout 15 ssh -n "$RDPU" "nohup python3 /tmp/vpm_sample.py $((END+WARM+30)) /tmp/vpm_series.csv 100 </dev/null >/dev/null 2>&1 & true" || true
for h in $SENDERS; do d=$(dpu_of $h); timeout 15 ssh -n "$d" "nohup bash /tmp/swift_poll.sh $((END+WARM+28)) 0.25 /tmp/swift_poll.log </dev/null >/dev/null 2>&1 & true" || true; done
k=0
while read -r sh sv dv ty n st en; do
  dur=$((en-st)); off=$((WARM+st)); [ "$st" -eq 0 ] && { dur=$((en+WARM)); off=0; }
  dip=$(ip_of $RECV $dv)
  if [ "$ty" = rdma ]; then
    cmd="$PT -d $(dev_of $sh $sv) -x 3 -m 1024 -q $n -p $((19300+k)) --report_gbits -D $dur --start_at=$((T0+off)) $dip"
  else
    cmd="iperf3 -c $dip -p $((5601+k)) -P $n -t $dur -C $TCPCC --start-at $((T0+off)) -J -B $(ip_of $sh $sv)%dpu1vf$sv"
  fi
  on_host "$sh" "$cmd" > "$DIR/flow${k}_${sh}vf${sv}_to_vf${dv}_${ty}.log" 2>&1 &
  k=$((k+1))
done <<<"$FLOWS"
LAUNCHED=$(date +%s); echo "$LAUNCHED" > "$DIR/launched.txt"
[ "$LAUNCHED" -lt "$T0" ] || echo "WARNING: clients launched $((LAUNCHED-T0)) s AFTER T0"
wait
date +%s.%N > "$DIR/t1.txt"
snap_cnp > "$DIR/cnp_post.txt"
sleep 6
scp -q "$RDPU:/tmp/vpm_series.csv" "$DIR/vpm_series.csv"
for h in $SENDERS; do d=$(dpu_of $h); until ssh -n $d 'test -f /tmp/swift_poll.log' </dev/null; do sleep 2; done; scp -q "$d:/tmp/swift_poll.log" "$DIR/poll_$d.log"; done
ssh "$RECV" 'pkill -x iperf3 2>/dev/null; true'
echo "$TCPCC" > "$DIR/tcpcc.txt"
echo "$TAG done -> $DIR"
