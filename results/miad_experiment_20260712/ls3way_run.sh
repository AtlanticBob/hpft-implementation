#!/usr/bin/env bash
# Large-scale 3-way: args = law mi_alpha beta ad_beta tag. Weighted matrix
# (L1b policy bottleneck: 4 pairs caps 40/30/20/10, class wts 7:3/6:4/5:5/
# 3:7, 90s) + churn (kill vf0 TCP @45, rejoin @75). %dev bind. Tag outputs.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
LAW=$1; MIA=$2; BETA=$3; ADB=$4; TAG=$5
CAP=(40 30 20 10); RW=(7 6 5 3); TW=(3 4 5 7)
cd "$REPO"
python3 - "$LAW" "$MIA" "$BETA" "$ADB" <<'EOF'
import json,sys
law,mia,beta,adb=sys.argv[1],float(sys.argv[2]),float(sys.argv[3]),float(sys.argv[4])
r=json.load(open("config/lab-registry.json"))
e=r["e_params"]; e["law_skeleton"]=law; e["mi_alpha"]=mia; e["beta"]=beta; e["ad_beta"]=adb; e["v_periods"]=4
CAP=[40,30,20,10]; RW=[7,6,5,3]; TW=[3,4,5,7]
for n in range(4):
    for side in ("sgpu01","sgpu02"):
        vm=r["policy"]["vms"]["%s/vf%d"%(side,n)]; vm["weight"]=1
        vm["max_rate_bps"]=CAP[n]*1000000000; vm["class_weights"]={"rdma":RW[n],"tcp":TW[n]}
json.dump(r,open("config/lab-registry.json","w"),indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
ssh hpft-dpu2 'ethtool p1|grep Speed'
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 5; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
# guard: nfs>=8
for n in 0 1 2 3; do ssh -f sgpu02 "$PT -d mlx5_$((6+n)) -p $((25700+n)) --report_gbits -D 6 >/tmp/g$n 2>&1"; done; sleep 1
for n in 0 1 2 3; do $PT -d mlx5_$((6+n)) -p $((25700+n)) --report_gbits -D 6 10.1.$n.2 >/dev/null 2>&1 & iperf3 -B "10.1.$n.1%dpu1vf$n" -c 10.1.$n.2 -p $((5201+4*n)) -P4 -b 0 -t 6 >/dev/null 2>&1 & done
sleep 4; gnfs=$(ssh hpft-dpu2 'tail -1 /tmp/hpft_rxagent_e.jsonl'|python3 -c "import json,sys;print(json.load(sys.stdin)['nfs'])"); echo "guard nfs=$gnfs"; wait 2>/dev/null; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
[ "$gnfs" -ge 8 ] || { echo "GUARD FAIL nfs=$gnfs"; exit 1; }
# run: 4 pairs, RDMA all 130s; TCP: vf1-3 steady, vf0 churns (on[0,45] off[45,75] on[75,130])
for n in 0 1 2 3; do ssh -f sgpu02 "$PT -d mlx5_$((6+n)) -p $((25710+n)) --report_gbits -D 130 >/tmp/ls$n 2>&1"; done; sleep 2
t0=$(date +%s.%N); echo "$t0" > "$DIR/${TAG}_t0.txt"; rm -f "$DIR/${TAG}_events.txt"; echo "$t0 start">>"$DIR/${TAG}_events.txt"
for n in 0 1 2 3; do $PT -d mlx5_$((6+n)) -p $((25710+n)) --report_gbits -D 130 10.1.$n.2 >"$DIR/${TAG}_rdma$n.log" 2>&1 & done
for n in 1 2 3; do iperf3 -B "10.1.$n.1%dpu1vf$n" -c 10.1.$n.2 -p $((5201+4*n)) -P4 -b 0 -t 130 -J >/dev/null 2>&1 & done
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 45 -J >/dev/null 2>&1 &
sleep 45; echo "$(date +%s.%N) vf0tcp_off">>"$DIR/${TAG}_events.txt"
sleep 30; echo "$(date +%s.%N) vf0tcp_on">>"$DIR/${TAG}_events.txt"
iperf3 -B "10.1.0.1%dpu1vf0" -c 10.1.0.2 -p 5201 -P4 -b 0 -t 55 -J >/dev/null 2>&1 &
sleep 55; echo "$(date +%s.%N) end">>"$DIR/${TAG}_events.txt"; wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${TAG}_rx.jsonl"
echo "${TAG}-ls-done"
