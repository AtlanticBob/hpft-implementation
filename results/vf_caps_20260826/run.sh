#!/usr/bin/env bash
# Does the 50G-per-VF double cap actually hold? One fresh VF pair
# (sgpu01/vf4 -> sgpu02/vf4), RDMA and TCP, under four arms:
#   none     both layers off        (control: what one VF can do on a 200G link)
#   devlink  sender devlink only    (uplink cap must bind, meter must see no drops)
#   meter    receiver meter only    (downlink policer must bind, drops must appear)
#   both     standing lab shape
# plus one incast point: three senders' vf4 -> sgpu02/vf4 with devlink on, so
# each sender is under its own 50G but the sum (150G offered) is not - only
# the receiver meter can hold the VF at 50G.
#
# Data: results/<arm>_<proto>/ with app output, meter stats before/after,
# RDMA uses 16 QPs: the PCC executor caps flows it has no budget for at 5G per
# QP (registry rdma_unknown_rate_bps), so 8 QPs would sit at 40G below the cap
# under test; 16 QPs put that ceiling at 80G, above it.
# devlink readback. usage: run.sh [arm ...]
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation; cd "$REPO"
OUT=$REPO/results/vf_caps_20260826/results; mkdir -p "$OUT"
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
DUR=20; RECV=sgpu02; RDPU=hpft-dpu2; DST=10.1.4.2; DSTDEV=mlx5_10; METER=15
dev_of() { python3 -c "
import json;r=json.load(open('config/lab-registry.json'))
print(next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }
on_host() { if [ "$1" = "$(hostname)" ]; then shift; bash -c "$*"; else h=$1; shift; ssh -o BatchMode=yes "$h" "$*"; fi; }

meter_snap() { ssh "$RDPU" "sudo ovs-ofctl -O OpenFlow13 meter-stats ovsbr-p1 2>/dev/null | grep -A1 'meter:$METER ' | tr '\n' ' '; echo"; }
devlink_snap() { ssh hpft-dpu "sudo devlink port function rate show pci/0000:03:00.1/262149 2>/dev/null"; }
p1_rx() { ssh "$RDPU" "ethtool -S p1 2>/dev/null | awk '/^ *rx_bytes_phy:/{print \$2}'"; }
# what the destination VF actually RECEIVED (after the meter): RoCE via the
# VF's IB port counter (4-byte units), TCP via the VF netdev
vf_rx() { ssh "$RECV" "echo \$(( \$(cat /sys/class/infiniband/$DSTDEV/ports/1/counters/port_rcv_data) * 4 + \$(cat /sys/class/net/dpu1vf4/statistics/rx_bytes) ))"; }

set_arm() {
  case "$1" in
    none)    bash tools/lab-infra/vf_caps.sh clear >/dev/null ;;
    devlink) bash tools/lab-infra/vf_caps.sh meter-off >/dev/null; bash tools/lab-infra/vf_caps.sh devlink-on >/dev/null ;;
    meter)   bash tools/lab-infra/vf_caps.sh devlink-off >/dev/null; bash tools/lab-infra/vf_caps.sh meter-on >/dev/null ;;
    both)    bash tools/lab-infra/vf_caps.sh sync >/dev/null ;;
  esac
}

run_point() { # $1 arm $2 proto (rdma|tcp) $3 senders (space list)
  local arm=$1 proto=$2 senders=$3 tag d
  tag=${arm}_${proto}$( [ "$(echo $senders | wc -w)" -gt 1 ] && echo _incast3 )
  d=$OUT/$tag; mkdir -p "$d"
  ssh "$RECV" 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; true'
  for s in $senders; do on_host "$s" 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; true'; done
  sleep 1
  local k=0
  for s in $senders; do
    if [ "$proto" = rdma ]; then
      ssh -o BatchMode=yes -f "$RECV" "$PT -d $DSTDEV -q 16 -p $((19100+k)) --report_gbits -D $DUR > /tmp/vfc_s$k.log 2>&1"
    else
      timeout 10 ssh -n "$RECV" "nohup iperf3 -s -p $((5401+k)) </dev/null >/dev/null 2>&1 & true" || true
    fi
    k=$((k+1))
  done
  sleep 2
  meter_snap > "$d/meter_pre.txt"; devlink_snap > "$d/devlink.txt"; p1_rx > "$d/p1rx_pre.txt"; vf_rx > "$d/vfrx_pre.txt"
  date +%s.%N > "$d/t0.txt"
  k=0
  for s in $senders; do
    if [ "$proto" = rdma ]; then
      on_host "$s" "$PT -d $(dev_of $s 4) -q 16 -p $((19100+k)) --report_gbits -D $DUR $DST" > "$d/rdma_$s.log" 2>&1 &
    else
      on_host "$s" "iperf3 -c $DST -p $((5401+k)) -P 16 -t $DUR -J -B 10.1.4.${s#sgpu0}%dpu1vf4" > "$d/tcp_$s.json" 2>&1 &
    fi
    k=$((k+1))
  done
  wait
  date +%s.%N > "$d/t1.txt"
  meter_snap > "$d/meter_post.txt"; p1_rx > "$d/p1rx_post.txt"; vf_rx > "$d/vfrx_post.txt"
  ssh "$RECV" 'pkill -x iperf3 2>/dev/null; true'
  # one-line digest
  local app=""
  for s in $senders; do
    if [ "$proto" = rdma ]; then
      app="$app $s=$(awk '/^ *[0-9]+ +[0-9]+ +[0-9.]+ +[0-9.]+ +[0-9.]+/{bw=$4} END{print bw}' "$d/rdma_$s.log")G"
    else
      app="$app $s=$(python3 -c "import json;print(round(json.load(open('$d/tcp_$s.json'))['end']['sum_received']['bits_per_second']/1e9,1))" 2>/dev/null)G"
    fi
  done
  local drop pre post
  post=$(grep -oE 'byte_count:[0-9]+' "$d/meter_post.txt" | tail -1 | cut -d: -f2)
  pre=$(grep -oE 'byte_count:[0-9]+' "$d/meter_pre.txt" | tail -1 | cut -d: -f2)
  drop=$(( ${post:-0} - ${pre:-0} ))     # no meter installed -> 0
  local mpre mpost; mpost=$(grep -oE 'byte_in_count:[0-9]+' "$d/meter_post.txt" | cut -d: -f2); mpre=$(grep -oE 'byte_in_count:[0-9]+' "$d/meter_pre.txt" | cut -d: -f2)
  local wire vfrx; wire=$(python3 -c "print(round(($(cat $d/p1rx_post.txt)-$(cat $d/p1rx_pre.txt))*8/($(cat $d/t1.txt)-$(cat $d/t0.txt))/1e9,1))")
  vfrx=$(python3 -c "print(round(($(cat $d/vfrx_post.txt)-$(cat $d/vfrx_pre.txt))*8/($(cat $d/t1.txt)-$(cat $d/t0.txt))/1e9,1))")
  echo "$tag | app goodput:$app | receiver p1 wire, before meter ${wire}G | delivered to VF, after meter ${vfrx}G | meter in_count delta $(( ${mpost:-0} - ${mpre:-0} )) B" | tee "$d/digest.txt"
}

ARMS=${*:-none devlink meter both}
for arm in $ARMS; do
  echo "== arm $arm =="; set_arm "$arm"; sleep 2
  run_point "$arm" rdma "sgpu01"
  run_point "$arm" tcp  "sgpu01"
done
for arm in $ARMS; do
  case $arm in devlink|both) ;; *) continue ;; esac
  echo "== incast: $arm, 3 senders -> one VF =="; set_arm $arm; sleep 2
  run_point $arm rdma "sgpu01 sgpu03 sgpu04"
  run_point $arm tcp  "sgpu01 sgpu03 sgpu04"
done
bash tools/lab-infra/vf_caps.sh sync >/dev/null    # leave the lab in the standing shape
echo "== done; standing shape re-asserted =="
