#!/usr/bin/env bash
# 28-fs three-way: args = law mi_alpha beta ad_beta tag. 12 RDMA cross-pairs
# (collision-free) + 16 TCP mesh, all 20G caps 1:1, 100s. %dev binding
# (class-attribution robustness). Fresh agents + settle + nfs guard.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LAW=$1; MIA=$2; BETA=$3; ADB=$4; TAG=$5
RDMA_PAIRS="0,0 0,1 0,2 0,3 1,0 1,1 1,3 2,2 3,0 3,1 3,2 3,3"; D=100
cd "$REPO"
python3 - "$LAW" "$MIA" "$BETA" "$ADB" <<'EOF'
import json,sys
law,mia,beta,adb=sys.argv[1],float(sys.argv[2]),float(sys.argv[3]),float(sys.argv[4])
r=json.load(open("config/lab-registry.json"))
e=r["e_params"]; e["law_skeleton"]=law; e["mi_alpha"]=mia; e["beta"]=beta; e["ad_beta"]=adb; e["v_periods"]=4
for vm,p in r["policy"]["vms"].items():
    p["max_rate_bps"]=20000000000; p["weight"]=1; p["class_weights"]={"tcp":1,"rdma":1}
json.dump(r,open("config/lab-registry.json","w"),indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
ssh hpft-dpu2 'ethtool p1|grep Speed'
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 6; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
SRV=""; for ij in $RDMA_PAIRS; do i=${ij%,*};j=${ij#*,};p=$((26200+4*i+j)); SRV+="nohup $PT -d mlx5_$((6+j)) -p $p --report_gbits -D $D >/tmp/f3_$i$j 2>&1 & "; done
ssh sgpu02 "$SRV true"; sleep 2
t0=$(date +%s.%N); echo "$t0" > "$DIR/${TAG}_t0.txt"
for ij in $RDMA_PAIRS; do i=${ij%,*};j=${ij#*,};p=$((26200+4*i+j)); $PT -d mlx5_$((6+i)) -p $p --report_gbits -D $D 10.1.$j.2 >/dev/null 2>&1 & sleep 0.3; done
for i in 0 1 2 3; do for j in 0 1 2 3; do iperf3 -B "10.1.$i.1%dpu1vf$i" -c 10.1.$j.2 -p $((5201+4*i+j)) -b 0 -t $((D-5)) -J >/dev/null 2>&1 & sleep 0.1; done; done
sleep $((D-8))
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${TAG}_rx.jsonl"
wait 2>/dev/null; echo "${TAG}-fair-done"
