#!/usr/bin/env bash
# Reproduce/characterize the multi-pair class-split oscillation. 4 pairs
# STEADY (no churn) at L1b caps 40/30/20/10G, class weights 7:3/6:4/5:5/3:7,
# 120s. If the class split within a pair spontaneously oscillates at high
# rate under multi-pair measurement noise (as seen 16s post-rejoin in L2),
# it will show here without any churn. Optional V override to test whether
# more marking tolerance damps it (like V=4 did for the D4 root layer).
# Usage: oscrepro_run.sh <Voverride|keep> <tag>
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
V=${1:-keep}; TAG=${2:-v4}

cd "$REPO"
python3 - "$V" <<'EOF'
import json, sys
r = json.load(open("config/lab-registry.json"))
CAP=[40,30,20,10]; RW=[7,6,5,3]; TW=[3,4,5,7]
if sys.argv[1] != "keep":
    r["e_params"]["v_periods"] = float(sys.argv[1])
for n in range(4):
    for side in ("sgpu01","sgpu02"):
        vm=r["policy"]["vms"]["%s/vf%d"%(side,n)]
        vm["weight"]=1; vm["max_rate_bps"]=CAP[n]*1000000000
        vm["class_weights"]={"rdma":RW[n],"tcp":TW[n]}
json.dump(r,open("config/lab-registry.json","w"),indent=2)
print("osc repro: caps 40/30/20/10, class {7:3,6:4,5:5,3:7}, V=%s" % (sys.argv[1]))
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json

ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
sleep 5
ssh hpft-dpu2 'ethtool p1 | grep Speed'
ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
SRV=""; for n in 0 1 2 3; do SRV+="nohup $PT -d mlx5_$((6+n)) -p $((24210+n)) --report_gbits -D 120 > /tmp/osc_s$n.log 2>&1 & "; done
ssh sgpu02 "$SRV true"; sleep 2
t0=$(date +%s.%N); echo "$t0" > "$DIR/osc_${TAG}_t0.txt"
for n in 0 1 2 3; do
    $PT -d mlx5_$((6+n)) -p $((24210+n)) --report_gbits -D 120 "10.1.$n.2" > "$DIR/osc_${TAG}_rdma$n.log" 2>&1 &
    iperf3 -B "10.1.$n.1%dpu1vf$n" -c "10.1.$n.2" -p $((5201+4*n)) -P4 -b 0 -t 120 -J > "$DIR/osc_${TAG}_tcp$n.json" 2>&1 &
    sleep 0.3
done
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/osc_${TAG}_rx.jsonl"
echo "osc-${TAG}-done"
