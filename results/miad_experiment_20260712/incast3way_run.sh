#!/usr/bin/env bash
# Incast root-regime 3-way: args = law mi_alpha beta ad_beta tag. Caps
# lifted, p1=100G -> root (97G) binds. 4 straight-pair RDMA equal weight,
# full demand, 90s. RDMA-only (no class attribution). Measures aggregate,
# per-flow sd, synchronization - does MIAD/MIMD hold ~98% like AIMD(V4) or
# oscillate like the pre-V4 root regime?
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LAW=$1; MIA=$2; BETA=$3; ADB=$4; TAG=$5
cd "$REPO"
python3 - "$LAW" "$MIA" "$BETA" "$ADB" <<'EOF'
import json,sys
law,mia,beta,adb=sys.argv[1],float(sys.argv[2]),float(sys.argv[3]),float(sys.argv[4])
r=json.load(open("config/lab-registry.json"))
e=r["e_params"]; e["law_skeleton"]=law; e["mi_alpha"]=mia; e["beta"]=beta; e["ad_beta"]=adb; e["v_periods"]=4
for vm,p in r["policy"]["vms"].items():
    p["max_rate_bps"]=None; p["weight"]=1; p["class_weights"]={"tcp":1,"rdma":1}
json.dump(r,open("config/lab-registry.json","w"),indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
cur=$(ssh hpft-dpu2 "ethtool p1|awk '/Speed/{print \$2}'")
[ "$cur" = "100000Mb/s" ] || { ssh hpft-dpu2 "sudo ethtool -s p1 speed 100000 duplex full" 2>/dev/null; sleep 15; }
ssh hpft-dpu2 'ethtool p1|grep Speed'; ssh hpft-dpu2 'ping -c3 -i0.2 -W1 10.1.9.1 2>&1|tail -1'
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 5; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
for n in 0 1 2 3; do ssh -f sgpu02 "$PT -d mlx5_$((6+n)) -p $((26000+n)) --report_gbits -D 90 >/tmp/ic$n 2>&1"; done; sleep 2
t0=$(date +%s.%N); echo "$t0" > "$DIR/${TAG}_t0.txt"
for n in 0 1 2 3; do $PT -d mlx5_$((6+n)) -p $((26000+n)) --report_gbits -D 90 10.1.$n.2 >"$DIR/${TAG}_vf$n.log" 2>&1 & done
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${TAG}_rx.jsonl"
echo "${TAG}-incast-done"
