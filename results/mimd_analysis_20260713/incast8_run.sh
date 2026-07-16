#!/usr/bin/env bash
# Proper TCP+RDMA incast (user spec). args = law md_anchor md_floor_frac tag.
# p1=100G root. 4 straight pairs vf{n}>vf{n}, EACH sends RDMA + TCP. All 8
# VMs max_rate=50G (sum 200>100 -> root binds), class 1:1. Expect each of 8
# flows ~12.5G (100/8). %dev TCP binding for attribution. 90s simultaneous.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LAW=$1; MDA=$2; MDF=$3; TAG=$4; CAPG=${5:-50}; TCA=${6:-}; QN=${7:-4}; D=90
cd "$REPO"
python3 - "$LAW" "$MDA" "$MDF" "$CAPG" <<'EOF'
import json,sys
law,mda,mdf,capg=sys.argv[1],sys.argv[2],float(sys.argv[3]),float(sys.argv[4])
r=json.load(open("config/lab-registry.json"))
e=r["e_params"]; e["law_skeleton"]=law; e["md_anchor"]=mda; e["md_floor_frac"]=mdf; e["v_periods"]=4
if law=="mimd": e["mi_alpha"]=0.05; e["beta"]=0.15
for vm,p in r["policy"]["vms"].items():
    p["max_rate_bps"]=int(capg*1e9); p["weight"]=1; p["class_weights"]={"tcp":1,"rdma":1}
json.dump(r,open("config/lab-registry.json","w"),indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
cur=$(ssh hpft-dpu2 "ethtool p1|awk '/Speed/{print \$2}'")
[ "$cur" = "100000Mb/s" ] || { ssh hpft-dpu2 "sudo ethtool -s p1 speed 100000 duplex full"; sleep 15; }
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 6; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
# RDMA servers (straight pairs, collision-free)
SRV=""; for n in 0 1 2 3; do SRV+="nohup $PT -d mlx5_$((6+n)) -p $((26400+n)) -q $QN --report_gbits -D $D >/tmp/i8_$n 2>&1 & "; done
ssh sgpu02 "$SRV true"; sleep 2
t0=$(date +%s.%N); echo "$t0" > "$DIR/${TAG}_t0.txt"
# RDMA clients + TCP (both classes per vf), all ~simultaneous
for n in 0 1 2 3; do $PT -d mlx5_$((6+n)) -p $((26400+n)) -q $QN $TCA --report_gbits -D $D 10.1.$n.2 >/dev/null 2>&1 & done
for n in 0 1 2 3; do iperf3 -B "10.1.$n.1%dpu1vf$n" -c 10.1.$n.2 -p $((5201+4*n)) -P$QN -b 0 -t $((D-5)) -J >/dev/null 2>&1 & done
sleep $((D-6))
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${TAG}_rx.jsonl"
wait 2>/dev/null; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; echo "${TAG}-incast8-done"
