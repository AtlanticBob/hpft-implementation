#!/usr/bin/env bash
# Large-scale L1: two-level weighted matrix, static. 4 straight VF pairs.
# Inter-tenant weights 4:3:2:1 (vf0..vf3). Per-pair class weights
# rdma:tcp = 7:3, 6:4, 5:5, 3:7. p1=100G forces root contention; caps
# lifted so root (97G) binds and the tenant weights divide it, then each
# pair's class weights split its tenant share. Each pair: RDMA (1 QP) +
# TCP (iperf3 -P4) full demand, 90s. Exercises all 3 water-filling layers.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
# class weights per pair: vf0 7:3, vf1 6:4, vf2 5:5, vf3 3:7 (rdma:tcp)
RW=(7 6 5 3); TW=(3 4 5 7); WT=(4 3 2 1)

distribute() {
    scp -q "$REPO/config/lab-registry.json" hpft-dpu:/tmp/lr.json
    ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/tmp/lr.json
    ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
    scp -q "$REPO/config/lab-registry.json" sgpu02:hpft/lab-registry.json
}

cd "$REPO"
python3 - <<'EOF'
import json
r = json.load(open("config/lab-registry.json"))
WT = [4, 3, 2, 1]; RW = [7, 6, 5, 3]; TW = [3, 4, 5, 7]
r["e_params"]["v_periods"] = 4
for n in range(4):
    for side in ("sgpu01", "sgpu02"):
        vm = r["policy"]["vms"]["%s/vf%d" % (side, n)]
        vm["weight"] = WT[n]
        vm["max_rate_bps"] = None        # lift caps: root binds
        vm["class_weights"] = {"rdma": RW[n], "tcp": TW[n]}
json.dump(r, open("config/lab-registry.json", "w"), indent=2)
print("L1 policy: W=4:3:2:1, class {7:3,6:4,5:5,3:7}, caps lifted")
EOF
distribute

# p1 -> 100G (single flap; skip if already 100G to avoid extra flaps)
cur=$(ssh hpft-dpu2 "ethtool p1 | awk '/Speed/{print \$2}'")
if [ "$cur" != "100000Mb/s" ]; then
    ssh hpft-dpu2 "sudo ethtool -s p1 speed 100000 duplex full" 2>/dev/null
    sleep 15
fi
ssh hpft-dpu2 "ethtool p1 | grep Speed"
ssh hpft-dpu2 'ping -c 5 -i 0.2 -W 1 10.1.9.1 2>&1 | tail -1'

ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' > /dev/null
ssh hpft-dpu 'bash /tmp/rp_service.sh start' > /dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' > /dev/null
sleep 4
ssh sgpu02 'pkill -f "ib_write_[b]"; true'

# servers on sgpu02
SRV=""
for n in 0 1 2 3; do
    SRV+="nohup $PT -d mlx5_$((6+n)) -p $((24000+n)) --report_gbits -D 90 > /tmp/l1_rsrv$n.log 2>&1 & "
done
ssh sgpu02 "$SRV true"
sleep 2

t0=$(date +%s.%N); echo "$t0" > "$DIR/l1_t0.txt"
for n in 0 1 2 3; do
    $PT -d mlx5_$((6+n)) -p $((24000+n)) --report_gbits -D 90 "10.1.$n.2" \
        > "$DIR/l1_rdma$n.log" 2>&1 &
    iperf3 -B "10.1.$n.1" -c "10.1.$n.2" -p $((5201+4*n)) -P 4 -b 0 -t 90 -J \
        > "$DIR/l1_tcp$n.json" 2>&1 &
    sleep 0.3
done
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/l1_rx.jsonl"
echo l1-done
