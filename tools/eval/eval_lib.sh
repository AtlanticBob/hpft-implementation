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

# Who is who, and how many. Every helper below loops over these instead of
# naming a DPU: the lab is four machines, any of them can receive, and a
# scenario registry pushed to two of four is the quietest way to run an
# experiment whose senders disagree about the policy.
EVAL_SENDER=${EVAL_SENDER:-$(python3 -c "import json;print(json.load(open('$REPO/config/lab-registry.json'))['sender_host'])")}
EVAL_RECEIVER=${EVAL_RECEIVER:-$(python3 -c "import json;print(json.load(open('$REPO/config/lab-registry.json'))['receiver_host'])")}
_all_dpus() { python3 -c "
import json;print(' '.join(n['dpu'] for n in json.load(open('$REPO/config/lab-registry.json'))['nodes']))"; }
_dpu_of()   { python3 -c "
import json;print({n['host']:n['dpu'] for n in json.load(open('$REPO/config/lab-registry.json'))['nodes']}['$1'])"; }
# senders default to the registry's single sender so existing runners keep
# their meaning; a four-node runner sets EVAL_SENDERS.
EVAL_SENDERS=${EVAL_SENDERS:-$EVAL_SENDER}
_dev_of() { python3 -c "
import json;r=json.load(open('$REPO/config/lab-registry.json'))
print(next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }
_ip_of()  { python3 -c "
import json;r=json.load(open('$REPO/config/lab-registry.json'))
print(next(v['ip'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2'))"; }

# ---- scenario registry ------------------------------------------------
# reg_push "<python mutations on dict d>"   (empty string = repo values)
reg_push() {
  python3 - <<EOF
import json
d=json.load(open('$REPO/config/lab-registry.json'))
$1
json.dump(d,open('/tmp/eval_scenario_registry.json','w'),indent=2)
EOF
  for d in $(_all_dpus); do scp -q /tmp/eval_scenario_registry.json "$d:/opt/hpft/lab-registry.json"; done
}
reg_restore() {
  for d in $(_all_dpus); do scp -q "$REPO/config/lab-registry.json" "$d:/opt/hpft/lab-registry.json"; done
}

# ---- HPFT stack -------------------------------------------------------
agents_restart() {  # both agents only, fresh jsonl. RP (and its per-flowtag
                    # budgets) untouched -- use between points of one group.
  # vport_meter FIRST: it wedges silently (DEVX context death), and a fresh
  # rx_agent mmaps whatever shm inode exists at startup -- restarting the
  # meter after the agent leaves the agent reading a dead file.
  ssh "$(_dpu_of $EVAL_RECEIVER)" 'sudo systemctl restart hpft-vport-meter 2>/dev/null; sleep 1
    sudo systemctl stop hpft-rxagent-e 2>/dev/null; sudo systemctl reset-failed hpft-rxagent-e 2>/dev/null
    sudo rm -f /tmp/hpft_rxagent_e.jsonl; sudo systemd-run --unit hpft-rxagent-e /usr/bin/python3 /opt/hpft/rx_agent.py --local-host '"$EVAL_RECEIVER" >/dev/null
  for h in $EVAL_SENDERS; do
    ssh "$(_dpu_of $h)" "sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null
      sudo rm -f /tmp/hpft_txagent_e.jsonl; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py --local-host $h" >/dev/null
  done
  sleep 5
}
stack_restart() {   # RP + both agents, fresh jsonl. RP restart CLEARS device
                    # budgets: seed them before any unpaced multi-QP launch.
  # roles.sh restarts each sender's RP before its agent, in that order: an
  # executor left from the previous point keeps taking budgets and quietly
  # stops pacing.
  bash "$REPO/tools/lab-infra/roles.sh" set --receiver "$EVAL_RECEIVER" \
       --senders "$(echo $EVAL_SENDERS | tr ' ' ',')" >/dev/null
  sleep 5
}
agents_grab() {     # agents_grab <outdir> <tag>
  scp -q "$(_dpu_of $EVAL_RECEIVER):/tmp/hpft_rxagent_e.jsonl" "$1/${2}_rx.jsonl" 2>/dev/null || true
  for h in $EVAL_SENDERS; do
    # one tx log per sender; the single-sender name is kept so the existing
    # analyzers still find it.
    if [ "$EVAL_SENDERS" = "$h" ]; then n="$1/${2}_tx.jsonl"; else n="$1/${2}_tx_${h}.jsonl"; fi
    scp -q "$(_dpu_of $h):/tmp/hpft_txagent_e.jsonl" "$n" 2>/dev/null || true
  done
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
  python3 - "$EVAL_SENDER" "$EVAL_RECEIVER" <<'EOF'
import json, sys
snd, rcv = sys.argv[1], sys.argv[2]
d = json.load(open("/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-registry.json"))
for h in (snd, rcv):
    d["policy"]["vms"][h + "/vf0"]["max_rate_bps"] = 20000000000
json.dump(d, open("/tmp/rpfresh_a.json", "w"), indent=2)
for h in (snd, rcv):
    d["policy"]["vms"][h + "/vf0"]["max_rate_bps"] = 8000000000
json.dump(d, open("/tmp/rpfresh_b.json", "w"), indent=2)
EOF
  for d in $(_all_dpus); do scp -q /tmp/rpfresh_a.json "$d:/opt/hpft/lab-registry.json"; done
  sleep 2
  local sdev rdev rip
  sdev=$(_dev_of "$EVAL_SENDER" 0); rdev=$(_dev_of "$EVAL_RECEIVER" 0); rip=$(_ip_of "$EVAL_RECEIVER" 0)
  rdma_server "$rdev" 28905 4 26; sleep 1.2
  rdma_client "$sdev" 28905 4 26 "$rip" /tmp/rpfresh.log &
  sleep 12
  for d in $(_all_dpus); do scp -q /tmp/rpfresh_b.json "$d:/opt/hpft/lab-registry.json"; done
  sleep 10
  local late
  late=$(ssh "$(_dpu_of $EVAL_RECEIVER)" "tail -400 /tmp/hpft_rxagent_e.jsonl" 2>/dev/null | FS="$EVAL_SENDER/vf0>$EVAL_RECEIVER/vf0|rdma" python3 -c "
import sys, json, os
key = os.environ['FS']
vals = []
for line in sys.stdin:
    try: r = json.loads(line)
    except ValueError: continue
    v = (r.get('r') or {}).get(key)
    if v: vals.append(v/1e9)
print(round(sum(vals[-40:])/max(len(vals[-40:]),1), 2) if vals else 99)")
  wait
  reg_restore
  traffic_clear
  echo "rp_fresh: wire ${late}G after 20G->8G step (want <9.5)"
  python3 -c "import sys; sys.exit(0 if float('$late') < 9.5 else 1)"
}

# ---- traffic ----------------------------------------------------------
traffic_clear() {
  for h in $EVAL_RECEIVER $EVAL_SENDERS; do
    [ "$h" = "$(hostname)" ] && continue
    ssh "$h" 'pkill -f "ib_write_[b]"; pkill -f "iperf[3]"; true' 2>/dev/null || true
  done
  pkill -f "ib_write_b[w]" 2>/dev/null || true
  sleep 1
}
rdma_server() {     # rdma_server <mlx5_N> <port> <qps> <dur>
  ssh -f "$EVAL_RECEIVER" "nohup $PT -d $1 -p $2 -q $3 --report_gbits -D $4 >/tmp/eval_rs_$2.log 2>&1"
}
rdma_client() {     # rdma_client <mlx5_N> <port> <qps> <dur> <dst_ip> <log> [extra...]
  local d=$1 p=$2 q=$3 dur=$4 ip=$5 log=$6; shift 6
  "$PT" -d "$d" -p "$p" -q "$q" --report_gbits -D "$dur" "$@" "$ip" > "$log" 2>&1
}
tcp_server() {      # tcp_server <port>
  ssh -f "$EVAL_RECEIVER" "nohup $IPERF -s -p $1 >/tmp/eval_is_$1.log 2>&1"
}
tcp_client() {      # tcp_client <port> <flows> <dur> <src_vf_idx> <dst_ip> <json_out>
  "$IPERF" -c "$5" -p "$1" -P "$2" -t "$3" -J -B "10.1.$4.1%dpu1vf$4" > "$6" 2>&1
}
tcp_client_cc() {   # same + 7th arg: congestion control (reno|cubic|bbr)
  "$IPERF" -c "$5" -p "$1" -P "$2" -t "$3" -J -B "10.1.$4.1%dpu1vf$4" -C "$7" > "$6" 2>&1
}

# ---- sampling / counters ----------------------------------------------
vpm_start() {       # vpm_start <dur_s> <interval_ms> -> remote csv /tmp/eval_vpm.csv
  scp -q "$REPO/tools/dpu/vpm_sample.py" "$(_dpu_of $EVAL_RECEIVER):/tmp/vpm_sample.py"
  ssh -f "$(_dpu_of $EVAL_RECEIVER)" "nohup python3 /tmp/vpm_sample.py $1 /tmp/eval_vpm.csv ${2:-1000} >/dev/null 2>&1"
}
vpm_grab() { scp -q "$(_dpu_of $EVAL_RECEIVER):/tmp/eval_vpm.csv" "$1/${2}_vpm.csv" 2>/dev/null || true; }
armstat_start() {   # armstat_start <dpu alias> <dur>
  scp -q "$REPO/tools/dpu/arm_stat.py" "$1":/tmp/arm_stat.py
  ssh -f "$1" "nohup python3 /tmp/arm_stat.py --duration $2 >/tmp/eval_armstat.csv 2>/dev/null"
}
armstat_grab() { scp -q "$2":/tmp/eval_armstat.csv "$1/${3}_armstat_${2}.csv" 2>/dev/null || true; }
counters_snap() {   # counters_snap <outfile>  (receiver IB + p1 phy + CNP set)
  # The PF's rdma device index is not the same on every machine, so it is
  # resolved from the registry's vnic list rather than pinned to mlx5_3.
  local RPF SPF
  RPF=$(ssh "$EVAL_RECEIVER" "readlink -f /sys/class/net/dpu1vf0/device/physfn 2>/dev/null | xargs -r basename")
  RPF=$(ssh "$EVAL_RECEIVER" "for d in /sys/class/infiniband/*; do [ \"\$(basename \$(readlink -f \$d/device))\" = \"$RPF\" ] && basename \$d && break; done")
  SPF=$(for d in /sys/class/infiniband/*; do
          [ -e "$d/device/net/dpu1vf0" ] || continue; done; echo mlx5_3)
  { ssh "$EVAL_RECEIVER" 'for c in port_rcv_data np_ecn_marked_roce_packets np_cnp_sent out_of_buffer; do
      echo "rx_$c $(cat /sys/class/infiniband/'"${RPF:-mlx5_3}"'/ports/1/hw_counters/$c 2>/dev/null || cat /sys/class/infiniband/'"${RPF:-mlx5_3}"'/ports/1/counters/$c 2>/dev/null)"; done'
    for c in rp_cnp_handled rp_cnp_ignored; do
      echo "tx_$c $(cat /sys/class/infiniband/${SPF:-mlx5_3}/ports/1/hw_counters/$c 2>/dev/null)"; done
    echo "p1_rx_phy $(ssh "$(_dpu_of $EVAL_RECEIVER)" "ethtool -S p1 | awk '/rx_bytes_phy:/{print \$2}'")"
  } > "$1"
}
stamp() { date +%s.%N > "$1"; }   # host stamp: coarse bookkeeping ONLY,
                                  # never for convergence metrics
