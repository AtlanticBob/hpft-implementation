#!/usr/bin/env bash
# Soft-floor collapse-safety. args = md_anchor md_floor_frac tag. law=mimd.
# Caps lifted, p1=100G root (~97G) binds. STAGGERED: vf0 RDMA [0,90] alone
# (~97G), vf1/2/3 join [30,90] -> vf0 share CRASHES 97->~24G. Tests whether
# a high soft floor slows the necessary give-way / overshoots aggregate.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); REPO=$(cd "$DIR/../.." && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
MDA=$1; MDF=$2; TAG=$3
cd "$REPO"
python3 - "$MDA" "$MDF" <<'EOF'
import json,sys
mda,mdf=sys.argv[1],float(sys.argv[2])
r=json.load(open("config/lab-registry.json"))
e=r["e_params"]; e["law_skeleton"]="mimd"; e["mi_alpha"]=0.05; e["beta"]=0.15
e["md_anchor"]=mda; e["md_floor_frac"]=mdf; e["v_periods"]=4
for vm,p in r["policy"]["vms"].items():
    p["max_rate_bps"]=None; p["weight"]=1; p["class_weights"]={"tcp":1,"rdma":1}
json.dump(r,open("config/lab-registry.json","w"),indent=2)
EOF
scp -q config/lab-registry.json hpft-dpu:/tmp/lr.json; ssh hpft-dpu 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json hpft-dpu2:/tmp/lr.json; ssh hpft-dpu2 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
scp -q config/lab-registry.json sgpu02:hpft/lab-registry.json
cur=$(ssh hpft-dpu2 "ethtool p1|awk '/Speed/{print \$2}'")
[ "$cur" = "100000Mb/s" ] || { ssh hpft-dpu2 "sudo ethtool -s p1 speed 100000 duplex full" 2>/dev/null; sleep 15; }
ssh hpft-dpu 'bash /tmp/rp_service.sh start' >/dev/null 2>&1
ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e; sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e; sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
sleep 5; ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
# vf0 server [0,90]; vf1-3 servers for [30,90]
ssh -f sgpu02 "$PT -d mlx5_6 -p 26000 --report_gbits -D 90 >/tmp/ci0 2>&1"; sleep 2
t0=$(date +%s.%N); echo "$t0" > "$DIR/${TAG}_t0.txt"
$PT -d mlx5_6 -p 26000 --report_gbits -D 90 10.1.0.2 >"$DIR/${TAG}_vf0.log" 2>&1 &
( sleep 30
  for n in 1 2 3; do ssh -f sgpu02 "$PT -d mlx5_$((6+n)) -p $((26000+n)) --report_gbits -D 58 >/tmp/ci$n 2>&1"; done; sleep 1
  for n in 1 2 3; do $PT -d mlx5_$((6+n)) -p $((26000+n)) --report_gbits -D 58 10.1.$n.2 >"$DIR/${TAG}_vf$n.log" 2>&1 & done
  wait ) &
wait
scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$DIR/${TAG}_rx.jsonl"
echo "${TAG}-crashincast-done"
