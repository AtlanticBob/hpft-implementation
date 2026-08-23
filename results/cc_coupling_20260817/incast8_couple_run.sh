#!/usr/bin/env bash
# incast8 with a selectable RDMA verb and executor coupling arm.
# pairs, vf_n -> vf_n, all contending for the receiver's 97G root.
# Same procedure as tools/tests/incast8_regression.sh; only the perftest
# binary and the output directory differ.
#
# usage: incast8_couple_run.sh <op: write|send|read> <tag>
#   COUPLE=0|1  executor coupling arm (mailbox 0xcca): 0 = min(cc, level),
#               1 = d * level. Unset leaves the device default (1).
#   CC_ALGO=0|1|2  CC term (0xccd): AIMD / ZTR / software DCQCN. Unset
#               leaves the device default (2, DCQCN).
#   FLOOR=<v>   d floor (0xcce <v> 4), fxp20: 1048576 = the whole share.
#   RECUS=<v>   recovery period in us (0xcce <v> 5).
#   PACEPCT=<v> achieved/programmed percentage above which a flow counts as
#               held back by us, so the CC's cut is not counted (0xcce <v> 6).
#   CAPACITY_BPS=<v>  deliverable downlink capacity for the receiver's
#               allocator, when it is not the port nameplate (registry
#               e_params.capacity_bps).
#   NHOLD=<n>   N hold window in epochs (0xcce <n> 3); 0 disables the hold
#               and restores the plain per-epoch QP count. Unset leaves the
#               device default (32).
#
# Data direction is sgpu01 -> sgpu02 for every verb. write/send: perftest
# server on sgpu02, client (the requester) on sgpu01. read: the CLIENT
# performs the READ, so the roles swap - server (responder, the one that
# transmits the READ responses) on sgpu01, client on sgpu02 - and the
# governed QPs are the sgpu01 responder QPs behind hpft-dpu's PCC.
set -u
OP=$1; TAG=$2; QN=4; D=90
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
OUT="$DIR/results"; mkdir -p "$OUT"
PT=$HOME/hyperfront/perftest-26015/ib_${OP}_bw
BIN=ib_${OP}_bw
cd "$REPO"
bash tools/lab-infra/deploy_check.sh >/dev/null || {
  bash tools/lab-infra/deploy_check.sh; echo "ABORT: lab not in a known state"; exit 1; }
# READ preflight: the responder side of a READ (server on sgpu01) is a
# transmit path the standing preflight never exercises. Observed 2026-08-16:
# sgpu01 VFs stop transmitting responder packets (ACKs, READ responses)
# while requester traffic still flows; the QP connects and moves 0 bytes.
# A VF rebuild on sgpu01 clears it. Probe each pair in sequence, rebuild
# once if any is dead, abort if still dead.
read_preflight() {
  local dead=0
  for n in 0 1 2 3; do
    ( $PT -F -d mlx5_$((6+n)) -p $((27960+n)) --report_gbits -D 2 >/dev/null 2>&1 & ); sleep 1.2
    bw=$(ssh sgpu02 "timeout 12 $PT -F -d mlx5_$((6+n)) -p $((27960+n)) --report_gbits -D 2 10.1.$n.1 2>&1 | grep -A1 'BW average' | tail -1 | awk '{print \$4}'")
    if [ -z "$bw" ] || awk "BEGIN{exit !($bw < 0.01)}"; then echo "  read vf$n responder DEAD (bw=${bw:-none})"; dead=1; else echo "  read vf$n ok ${bw}G"; fi
    pkill -f "ib_read_b[w]" 2>/dev/null; sleep 0.3
  done
  return $dead
}
if [ "$OP" = read ]; then
  read_preflight || {
    echo "== rebuilding sgpu01 VFs (responder wedge) =="
    bash tools/lab-infra/vf_setup.sh >/dev/null 2>&1; sleep 3
    for d in dpu1vf0 dpu1vf1 dpu1vf2 dpu1vf3; do sudo ip link set $d mtu 1500; done
    # new netdevs: re-attach TCP EDT (fq + BPF) and re-seed the pace shim
    sudo bash tools/host/edt_ensure.sh >/dev/null 2>&1; bash tools/host/edt_maps_ensure.sh >/dev/null 2>&1
    sudo systemctl stop hpft-pace-shim 2>/dev/null; sudo systemctl reset-failed hpft-pace-shim 2>/dev/null
    sudo systemd-run --unit hpft-pace-shim --property=Restart=always /usr/bin/python3 $REPO/tools/host/hpft_pace_shim.py >/dev/null 2>&1; sleep 2
    read_preflight || { echo "ABORT: READ responder path dead after VF rebuild"; exit 1; }
  }
fi
restore_standing() {
  scp -q "$REPO/config/lab-registry.json" hpft-dpu:/opt/hpft/  2>/dev/null || true
  scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/opt/hpft/ 2>/dev/null || true
}
trap restore_standing EXIT
python3 - <<'PY'
import json, os
r = json.load(open("config/lab-registry.json"))
for vm, p in r["policy"]["vms"].items():
    p["max_rate_bps"] = 50000000000
    p["weight"] = 1
    p["class_weights"] = {"tcp": 1, "rdma": 1}
cap = os.environ.get("CAPACITY_BPS")
if cap:
    r["e_params"]["capacity_bps"] = int(cap)
json.dump(r, open("/tmp/lr_i8.json", "w"), indent=2)
PY
scp -q /tmp/lr_i8.json hpft-dpu:/tmp/lr.json;  ssh hpft-dpu  'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q /tmp/lr_i8.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
cur=$(ssh hpft-dpu2 "ethtool p1|awk '/Speed/{print \$2}'")
[ "$cur" = "100000Mb/s" ] || { echo "!! p1 is $cur"; exit 1; }
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
# CC term under the level control (mailbox 0xccd): 0 = AIMD (07-28 term),
# 2 = software DCQCN (device default since 2026-08-04), 1 = ZTR. Unset =
# leave the device default.
if [ -n "${CC_ALGO:-}" ]; then ssh hpft-dpu "echo '0xccd $CC_ALGO' > /tmp/rp_fifo"; echo "cc_algo=$CC_ALGO"; fi
# Coupling arm. Sent AFTER the CC selection because both reset the per-pair
# CC state and the later write must be the one that lands.
if [ -n "${COUPLE:-}" ]; then ssh hpft-dpu "echo '0xcca $COUPLE' > /tmp/rp_fifo"; echo "couple=$COUPLE"; fi
if [ -n "${NHOLD:-}" ]; then ssh hpft-dpu "echo '0xcce $NHOLD 3' > /tmp/rp_fifo"; echo "nhold=$NHOLD"; fi
if [ -n "${FLOOR:-}" ]; then ssh hpft-dpu "echo '0xcce $FLOOR 4' > /tmp/rp_fifo"; echo "floor=$FLOOR"; fi
if [ -n "${RECUS:-}" ]; then ssh hpft-dpu "echo '0xcce $RECUS 5' > /tmp/rp_fifo"; echo "rec_us=$RECUS"; fi
if [ -n "${PACEPCT:-}" ]; then ssh hpft-dpu "echo '0xcce $PACEPCT 6' > /tmp/rp_fifo"; echo "pace_pct=$PACEPCT"; fi
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; true'
ssh hpft-dpu  'sudo systemctl stop hpft-txagent-e 2>/dev/null; true'
sleep 1
ssh hpft-dpu2 'sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 3
ssh hpft-dpu  'sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
sleep 4
# Device-side probe (PCC RP mailbox): 0xdeb p -> {flowtag,budget,level,
# remote_rx,r_units,cc_rate,epochs,cap}; 0xdec p -> {cnp_any,cnp_hits,
# qp_count,cc_rate,dbg_hits,flowtag,rtt_last,rtt_min}. dbg_hits/qp_count
# growing on a pair is the direct evidence that PCC receives events for
# (and governs) the QPs of that verb.
start_probe() {
  ssh hpft-dpu 'echo "PROBE_START $(date +%s.%N)" > /tmp/rp_probe_mark;
    ( for i in $(seq 1 40); do
        for p in 0 1 2 3 4 5 6 7; do
          echo "0xdeb $p" > /tmp/rp_fifo; echo "0xded $p" > /tmp/rp_fifo
        done
        sleep 2
      done ) >/dev/null 2>&1 &' &
}
ssh sgpu02 "pkill -f 'ib_[a-z]*_bw'; true"; pkill -f 'ib_[a-z]*_b[w]' 2>/dev/null; sleep 1
RPL0=$(ssh hpft-dpu 'wc -l < /tmp/pcc_rp.log 2>/dev/null || echo 0')
if [ "$OP" = read ]; then
  # responder (server) on sgpu01, requester (client) on sgpu02
  for n in 0 1 2 3; do $PT -F -d mlx5_$((6+n)) -p $((26400+n)) -q $QN --report_gbits -D $D >"$OUT/${TAG}_pt_$n.log" 2>&1 & done
  sleep 2
  t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"; start_probe
  CLI=""; for n in 0 1 2 3; do CLI+="nohup $PT -F -d mlx5_$((6+n)) -p $((26400+n)) -q $QN --report_gbits -D $D 10.1.$n.1 >/tmp/i8_$n 2>&1 & "; done
  ssh sgpu02 "$CLI true"
else
  SRV=""; for n in 0 1 2 3; do SRV+="nohup $PT -F -d mlx5_$((6+n)) -p $((26400+n)) -q $QN --report_gbits -D $D >/tmp/i8_$n 2>&1 & "; done
  ssh sgpu02 "$SRV true"
  sleep 2
  t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"; start_probe
  for n in 0 1 2 3; do $PT -F -d mlx5_$((6+n)) -p $((26400+n)) -q $QN --report_gbits -D $D 10.1.$n.2 >"$OUT/${TAG}_pt_$n.log" 2>&1 & done
fi
for n in 0 1 2 3; do iperf3 -B "10.1.$n.1%dpu1vf$n" -c 10.1.$n.2 -p $((5301+n)) -P$QN -b 0 -t $((D-5)) -J >"$OUT/${TAG}_iperf_$n.json" 2>&1 & done
sleep $((D-6))
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$OUT/${TAG}_rx.jsonl"
scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl  "$OUT/${TAG}_tx.jsonl"
wait 2>/dev/null
ssh sgpu02 "pkill -f 'ib_[a-z]*_bw'; true"
for n in 0 1 2 3; do scp -q sgpu02:/tmp/i8_$n "$OUT/${TAG}_peer_$n.log" 2>/dev/null || true; done
ssh hpft-dpu "pkill -f "0xde[b]" 2>/dev/null; tail -n +$((RPL0+1)) /tmp/pcc_rp.log | grep -aE 'HPFT_RSP|ft=0xde'" > "$OUT/${TAG}_rp.txt" 2>/dev/null
echo "${TAG}-incast8-${OP}-couple${COUPLE:-def}-done"
