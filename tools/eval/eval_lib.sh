#!/usr/bin/env bash
# Shared orchestration library for the eval campaign runners (P7).
# Source this from an experiment runner:  . "$REPO/tools/eval/eval_lib.sh"
# Distilled from the proven patterns in results/p0_overlay_20260729 and
# results/vtune_20260729. Conventions it encodes:
#   - pkill and server-start go over SEPARATE ssh sessions (a compound
#     "pkill; nohup server" under ssh -f leaves no server behind);
#   - scenario registries are rendered from the REPO copy, pushed to both
#     DPUs, and restored by trap (repo copy is never touched);
#   - agents restart via the systemd-run transient-unit pattern (stop +
#     reset-failed first, or the unit "already exists");
#   - RP restart CLEARS device budgets -- after stack_restart, budgets
#     exist only for flow-sets the tx agent has seen telemetry for;
#   - event times for convergence metrics must come from the telemetry's
#     own clock (host<->DPU stamp skew ~ +-0.3 s), see vtune summary.
# Every function takes explicit args; the only globals are REPO/PT.
set -u
REPO=${REPO:-/home/zhaoxiang/hyperfront/hpft-implementation}
PT=${PT:-$HOME/hyperfront/perftest-enhanced/ib_write_bw}
IPERF=${IPERF:-iperf3}

# ---- scenario registry ------------------------------------------------
# reg_push "<python mutations on dict d>"   (empty string = repo values)
reg_push() {
  python3 - <<EOF
import json
d=json.load(open('$REPO/config/lab-registry.json'))
$1
json.dump(d,open('/tmp/eval_scenario_registry.json','w'),indent=2)
EOF
  scp -q /tmp/eval_scenario_registry.json hpft-dpu:/opt/hpft/lab-registry.json
  scp -q /tmp/eval_scenario_registry.json hpft-dpu2:/opt/hpft/lab-registry.json
}
reg_restore() {
  scp -q "$REPO/config/lab-registry.json" hpft-dpu:/opt/hpft/lab-registry.json
  scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/opt/hpft/lab-registry.json
}

# ---- HPFT stack -------------------------------------------------------
agents_restart() {  # both agents only, fresh jsonl. RP (and its per-flowtag
                    # budgets) untouched -- use between points of one group.
  # vport_meter FIRST: it wedges silently (DEVX context death), and a fresh
  # rx_agent mmaps whatever shm inode exists at startup -- restarting the
  # meter after the agent leaves the agent reading a dead file.
  ssh hpft-dpu2 'sudo systemctl restart hpft-vport-meter 2>/dev/null; sleep 1
    sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null
    sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
  ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null
    sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
  sleep 5
}
stack_restart() {   # RP + both agents, fresh jsonl. RP restart CLEARS device
                    # budgets: seed them before any unpaced multi-QP launch.
  ssh hpft-dpu 'bash /tmp/rp_service.sh start >/dev/null 2>&1; true'
  ssh hpft-dpu2 'sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null
    sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py' >/dev/null
  ssh hpft-dpu 'sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null
    sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py' >/dev/null
  sleep 5
}
agents_grab() {     # agents_grab <outdir> <tag>
  scp -q hpft-dpu2:/tmp/hpft_rxagent_e.jsonl "$1/${2}_rx.jsonl" 2>/dev/null || true
  scp -q hpft-dpu:/tmp/hpft_txagent_e.jsonl "$1/${2}_tx.jsonl" 2>/dev/null || true
}

dataplane_guard() { # ops disease: the VF<->VF inner path can drop dead after
                    # a stack_restart (host-VF egress stops reaching the
                    # representor; underlay stays up). lab_env hpft's full
                    # re-assert heals it. Check all four pairs, heal once.
  local ok=1 i
  for i in 0 1 2 3; do
    timeout 3 ping -c 1 -W 1 -I dpu1vf$i 10.1.$i.2 >/dev/null 2>&1 || ok=0
  done
  [ $ok = 1 ] && return 0
  echo "dataplane_guard: dead inner path -- re-asserting via lab_env hpft"
  bash "$REPO/tools/lab_env.sh" hpft >/dev/null 2>&1
  for i in 0 1 2 3; do
    timeout 3 ping -c 1 -W 1 -I dpu1vf$i 10.1.$i.2 >/dev/null 2>&1 || return 1
  done
  return 0
}
rp_guard() {        # enforcement guard (ops disease family #3: RP state
                    # degrades with experiment churn; a degraded RP fails
                    # open silently while every layer reports healthy).
                    # Two 1-QP flows -> dst vf0 (30G cap, rdma-only => 10 --
                    # ~15G each enforced; ~26G each = fail-open). Returns 1
                    # on fail-open so the caller can restart the RP+retry.
  traffic_clear
  rdma_server mlx5_6 28901 1 8; rdma_server mlx5_6 28902 1 8; sleep 1.2
  rdma_client mlx5_6 28901 1 8 10.1.0.2 /tmp/guard0.log &
  rdma_client mlx5_9 28902 1 8 10.1.0.2 /tmp/guard1.log &
  wait
  local g0 g1
  g0=$(grep " 65536" /tmp/guard0.log | awk '{print int($4)}'); g0=${g0:-99}
  g1=$(grep " 65536" /tmp/guard1.log | awk '{print int($4)}'); g1=${g1:-99}
  traffic_clear
  echo "rp_guard: $g0 / $g1 G"
  [ "$g0" -le 20 ] && [ "$g1" -le 20 ]
}

rp_fresh() {  # budget-freshness probe (ops_notes family 3, form "stale
              # application"): the guard above only asks "is the executor
              # enforcing SOMETHING" -- it cannot see an executor still
              # applying a budget from minutes ago, which is exactly the
              # form that produced 12-24 s convergence tails. This probe
              # changes the budget mid-flight and checks the wire follows.
              # Returns 1 if the executor did not track the change.
  traffic_clear
  local REPO=/home/zhaoxiang/hyperfront/hpft-implementation
  python3 - <<'EOF'
import json
d = json.load(open("/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-registry.json"))
for h in ("sgpu01", "sgpu02"):
    d["policy"]["vms"][h + "/vf0"]["max_rate_bps"] = 20000000000
json.dump(d, open("/tmp/rpfresh_a.json", "w"), indent=2)
for h in ("sgpu01", "sgpu02"):
    d["policy"]["vms"][h + "/vf0"]["max_rate_bps"] = 8000000000
json.dump(d, open("/tmp/rpfresh_b.json", "w"), indent=2)
EOF
  scp -q /tmp/rpfresh_a.json hpft-dpu:/opt/hpft/lab-registry.json
  scp -q /tmp/rpfresh_a.json hpft-dpu2:/opt/hpft/lab-registry.json
  sleep 2
  rdma_server mlx5_6 28905 4 26; sleep 1.2
  rdma_client mlx5_6 28905 4 26 10.1.0.2 /tmp/rpfresh.log &
  sleep 12
  scp -q /tmp/rpfresh_b.json hpft-dpu:/opt/hpft/lab-registry.json
  scp -q /tmp/rpfresh_b.json hpft-dpu2:/opt/hpft/lab-registry.json
  sleep 10
  local late
  late=$(ssh hpft-dpu2 "tail -400 /tmp/hpft_rxagent_e.jsonl" 2>/dev/null | python3 -c "
import sys, json
vals = []
for line in sys.stdin:
    try: r = json.loads(line)
    except ValueError: continue
    v = (r.get('r') or {}).get('sgpu01/vf0>sgpu02/vf0|rdma')
    if v: vals.append(v/1e9)
print(round(sum(vals[-40:])/max(len(vals[-40:]),1), 2) if vals else 99)")
  wait
  scp -q "$REPO/config/lab-registry.json" hpft-dpu:/opt/hpft/lab-registry.json
  scp -q "$REPO/config/lab-registry.json" hpft-dpu2:/opt/hpft/lab-registry.json
  traffic_clear
  echo "rp_fresh: wire ${late}G after 20G->8G step (want <9.5)"
  python3 -c "import sys; sys.exit(0 if float('$late') < 9.5 else 1)"
}

# ---- traffic ----------------------------------------------------------
traffic_clear() {
  ssh sgpu02 'pkill -f "ib_write_[b]"; pkill -f "iperf[3]"; true' 2>/dev/null || true
  pkill -f "ib_write_b[w]" 2>/dev/null || true
  sleep 1
}
rdma_server() {     # rdma_server <mlx5_N> <port> <qps> <dur>
  ssh -f sgpu02 "nohup $PT -d $1 -p $2 -q $3 --report_gbits -D $4 >/tmp/eval_rs_$2.log 2>&1"
}
rdma_client() {     # rdma_client <mlx5_N> <port> <qps> <dur> <dst_ip> <log> [extra...]
  local d=$1 p=$2 q=$3 dur=$4 ip=$5 log=$6; shift 6
  "$PT" -d "$d" -p "$p" -q "$q" --report_gbits -D "$dur" "$@" "$ip" > "$log" 2>&1
}
tcp_server() {      # tcp_server <port>
  ssh -f sgpu02 "nohup $IPERF -s -p $1 >/tmp/eval_is_$1.log 2>&1"
}
tcp_client() {      # tcp_client <port> <flows> <dur> <src_vf_idx> <dst_ip> <json_out>
  "$IPERF" -c "$5" -p "$1" -P "$2" -t "$3" -J -B "10.1.$4.1%dpu1vf$4" > "$6" 2>&1
}
tcp_client_cc() {   # same + 7th arg: congestion control (reno|cubic|bbr)
  "$IPERF" -c "$5" -p "$1" -P "$2" -t "$3" -J -B "10.1.$4.1%dpu1vf$4" -C "$7" > "$6" 2>&1
}

# ---- sampling / counters ----------------------------------------------
vpm_start() {       # vpm_start <dur_s> <interval_ms> -> remote csv /tmp/eval_vpm.csv
  scp -q "$REPO/tools/dpu/vpm_sample.py" hpft-dpu2:/tmp/vpm_sample.py
  ssh -f hpft-dpu2 "nohup python3 /tmp/vpm_sample.py $1 /tmp/eval_vpm.csv ${2:-1000} >/dev/null 2>&1"
}
vpm_grab() { scp -q hpft-dpu2:/tmp/eval_vpm.csv "$1/${2}_vpm.csv" 2>/dev/null || true; }
armstat_start() {   # armstat_start <host:hpft-dpu|hpft-dpu2> <dur>
  scp -q "$REPO/tools/dpu/arm_stat.py" "$1":/tmp/arm_stat.py
  ssh -f "$1" "nohup python3 /tmp/arm_stat.py --duration $2 >/tmp/eval_armstat.csv 2>/dev/null"
}
armstat_grab() { scp -q "$2":/tmp/eval_armstat.csv "$1/${3}_armstat_${2}.csv" 2>/dev/null || true; }
counters_snap() {   # counters_snap <outfile>  (receiver IB + p1 phy + CNP set)
  { ssh sgpu02 'for c in port_rcv_data np_ecn_marked_roce_packets np_cnp_sent out_of_buffer; do
      echo "rx_$c $(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null || cat /sys/class/infiniband/mlx5_3/ports/1/counters/$c 2>/dev/null)"; done'
    for c in rp_cnp_handled rp_cnp_ignored; do
      echo "tx_$c $(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done
    echo "p1_rx_phy $(ssh hpft-dpu2 "ethtool -S p1 | awk '/rx_bytes_phy:/{print \$2}'")"
  } > "$1"
}
stamp() { date +%s.%N > "$1"; }   # host stamp: coarse bookkeeping ONLY,
                                  # never for convergence metrics
