#!/usr/bin/env bash
# validation runner: one scenario, one tag.
#
#   run.sh <scenario> <tag>        scenario = a file name in ../scenarios (without .flows)
#                                  -> results/<tag>/
#
# What it does, in order: refuses to run unless the lab matches the repo
# (deploy_check) and the standing registry carries the standard policy;
# puts every node in both planes (roles.sh all); starts one listener per
# flow-table row on that row's destination host; starts a vport-meter
# sampler on every receiving DPU; launches every row from its own host
# so that its first packet lands at T0 + warm-up + start (RDMA: perftest
# --start_at on both ends; TCP: iperf3 --start-at); collects the three
# data paths (vport-meter series, per-flow application logs, agent jsonl)
# plus the executor/CNP/switch snapshots; restores nothing because it changed
# nothing standing. A flow-table row names its destination host, so a run
# may have several receivers (2026-09-04): each gets its own listeners,
# sampler and agent log (rx_<host>.jsonl, vpm_series_<host>.csv).
#
# Time base: T0 is the absolute second the t=0 rows begin; the experiment
# clock (the one every table and figure uses) is T0 + WARM. A row with
# start s begins at T0 + WARM + s and runs until T0 + WARM + end.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); VAL=$(cd "$DIR/.." && pwd); REPO=$(cd "$VAL/.." && pwd)
SCN=${1:?scenario}; TAG=${2:?tag}
FLOWS="$VAL/scenarios/$SCN.flows"; [ -f "$FLOWS" ] || { echo "no such scenario: $FLOWS"; exit 1; }
OUT="$VAL/results/$TAG"; mkdir -p "$OUT"
PT=$HOME/hyperfront/perftest-enhanced/ib_write_bw
WARM=5; QMTU=1024
cd "$REPO"

reg() { python3 -c "import json,sys;r=json.load(open('config/lab-registry.json'));print($1)"; }
dev_of() { reg "next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }
ip_of()  { reg "next(v['ip'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }
rep_of() { reg "next(v['representor'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }
dpu_of() { reg "{n['host']:n['dpu'] for n in r['nodes']}['$1']"; }
on_host() { if [ "$1" = "$(hostname)" ]; then shift; bash -c "$*"; else h=$1; shift; ssh -n -o BatchMode=yes "$h" "$*" </dev/null; fi; }
# Message size for a rate-limited RDMA row, on BOTH ends (perftest refuses to
# negotiate a mismatch). perftest's SW rate limiter - the HW one is refused,
# the PCC executor owns the QP's rate, and packet pacing is Raw-Ethernet only
# - posts burst_size messages then busy-waits out the gap, and burst_size
# defaults to tx_depth = 128. At the default 64 KB message that is one 8.4 MB
# burst per 6.7 ms, so a 20 ms telemetry sample holds 3 of them and quantises
# to +-33%: an application-limited flow reads as ~18% jitter while its 100 ms
# wire rate is smooth to 0.9%, and no event of it can ever hold a +-10% band.
# Shrinking the MESSAGE shortens the burst in the same proportion and leaves
# 128 messages in flight. Shrinking burst_size instead left too few in flight
# to fill a fence sitting at its 2 G floor, so the cap (twice the flow's own
# use) never lifted and the fence stayed on the floor for a whole phase -
# measured r23, 0.26 G delivered of a 10 G demand.
rl_size() { case "$1" in rate_limit=*) echo "-s 8192" ;; esac; }

# ---- the flow table -------------------------------------------------------
ROWS=$(grep -vE '^\s*(#|$)' "$FLOWS")
# columns: src_host src_vf dst_host dst_vf class count start_s end_s options.
# class=meter is not a flow: it is the hidden bottleneck (README V8), applied
# on the destination's DPU; its source columns are '-'. class=core is not a
# flow either: it tells distill.py the core link's capacity (README V8 core
# version); the runner does nothing with it, all its host columns are '-'.
SENDERS=$(echo "$ROWS" | awk '$5!="meter" && $5!="core"{print $1}' | sort -u | tr '\n' ' ')
RECVS=$(echo "$ROWS" | awk '$3!="-"{print $3}' | sort -u | tr '\n' ' ')
HOSTS=$(echo $RECVS $SENDERS | tr ' ' '\n' | sort -u | tr '\n' ' ')
END=$(echo "$ROWS" | awk 'BEGIN{m=0}{if($8>m)m=$8}END{print m}')
cp "$FLOWS" "$OUT/flows.txt"; echo "$WARM" > "$OUT/warm.txt"

# ---- one run at a time, lab in a known state ------------------------------
LOCK=/tmp/hpft_run.lock; exec 9>"$LOCK"
flock -n 9 || { echo "ABORT: another run holds $LOCK"; exit 1; }
echo "$TAG $$" >&9
bash tools/lab-infra/deploy_check.sh >/dev/null || { bash tools/lab-infra/deploy_check.sh; echo "ABORT: lab does not match the repo"; exit 1; }
python3 - <<'EOF' || { echo "ABORT: standing registry is not the standard validation policy"; exit 1; }
import json, sys
r = json.load(open("config/lab-registry.json"))
bad = [v for v, p in r["policy"]["vms"].items()
       if p.get("weight") != 1 or p.get("max_rate_bps") != 50000000000 or p.get("class_weights") != {"tcp": 1, "rdma": 1}]
if bad or r["policy"].get("per_sender_weights"):
    print("non-standard policy:", bad, r["policy"].get("per_sender_weights")); sys.exit(1)
EOF
# The RP mailbox costs either ~13.3 ms or ~21.9 ms per send, switching
# between the two on a ~40 min cycle that we have not traced to anything on
# the DPU (load, interrupts, context switches and temperature are all flat
# across a transition) and that survives a doca_pcc restart. The call is
# blocking, so the slow mode cuts how often a fence can be pushed to the
# RDMA executor from 74/s to 46/s. It is NOT a delay in the control path -
# the wire responds 6 ms after the command, well before the call returns.
#
# Runs are no longer pinned to the fast mode (2026-08-31). Each DPU spends
# only a third of its time fast and the DPUs are on different periods, so a
# window with two senders fast at once lasts 4-7 min and comes round every
# ~35 min - a cost worth paying only if the mode changes results, which has
# not been shown and became much less likely once the QP-count miscount was
# fixed. So measure it, record it per run, and let the runs proceed: with
# the mode in results/<tag>/mailbox_mode.txt any two runs of one scenario
# can be compared across modes after the fact.
for h in $SENDERS; do
  d=$(dpu_of $h)
  # An idle lab has no budgets to push, so there may be nothing recent to
  # read. Ask directly then: 0xded is a pure readback and costs one mailbox
  # slot each, which is exactly what we want to time.
  mbcost="tail -400 /tmp/pcc_rp.log 2>/dev/null | grep -a HPFT_SET | sed -n 's/.*send_ns=\\([0-9]*\\).*/\\1/p' | sort -n | awk '{a[NR]=\$1} END{if(NR>20) printf \"%.1f\", a[int(NR/2)+1]/1e6}'"
  mbms=$(ssh -n -o BatchMode=yes "$d" "$mbcost" 2>/dev/null)
  if [ -z "$mbms" ]; then
    ssh -n -o BatchMode=yes "$d" "exec 3>/tmp/rp_fifo; for i in \$(seq 1 30); do echo '0xded 0' >&3; sleep 0.02; done; sleep 1" 2>/dev/null
    mbms=$(ssh -n -o BatchMode=yes "$d" "$mbcost" 2>/dev/null)
  fi
  [ -n "$mbms" ] || { echo "$d mailbox unknown" >> "$OUT/mailbox_mode.txt"; echo "WARN: $d mailbox mode could not be measured"; continue; }
  mode=fast; awk -v v="$mbms" 'BEGIN{exit !(v>17)}' && mode=slow
  echo "$d $mbms $mode" >> "$OUT/mailbox_mode.txt"
  echo "$d mailbox ${mbms} ms/send ($mode)"
done
cp config/lab-registry.json "$OUT/registry.json"
for h in $HOSTS; do
  d=$(dpu_of $h); cur=$(ssh -o BatchMode=yes "$d" 'cat /sys/class/net/p1/speed' 2>/dev/null)
  [ "$cur" = "200000" ] || { echo "ABORT: $d p1 is ${cur} Mb, expected 200000"; exit 1; }
done
# environment as it actually is (UPCC, SR, p1 speed, agents, switch ECN).
# The report copies these lines verbatim. NOTHING checks them - an earlier
# comment here claimed distill verified the SR column and it never did, which
# is how every run to date went out under GBN while README and EXECUTION both
# specify SR. Read the retransmission line below before trusting a report's
# environment table.
bash tools/lab_env.sh status > "$OUT/env_status.txt" 2>&1
# The SR column in that status line is mlxconfig RDMA_SELECTIVE_REPEAT_EN, and
# it is permanently 0 here because that knob needs a fw reset and the lab uses
# the other mechanism: the volatile ROCE_ACCL register selective_repeat_forced_en,
# which cc_mode.sh sets in a second. Reading the mlxconfig field to decide "is
# this run SR" is a category error - it cannot distinguish SR from GBN at all.
# Every HPFT experiment is specified to run on SR, so read the register that
# actually decides it, record it, and refuse to run without it.
for h in $HOSTS; do
  bdf=$(python3 -c "
import json
tcp=json.load(open('config/lab-tcp-registry.json'))
print({v['host']: v['pf_bdf'] for v in tcp['vnics']}.get('$h','0000:38:00.1').replace('0000:',''))")
  v=$(on_host "$h" "sudo mlxreg -y -d $bdf --reg_name ROCE_ACCL --get 2>/dev/null | awk '/selective_repeat_forced_en/{print \$NF}'" | head -1)
  echo "$h $bdf $v" >> "$OUT/retrans_mode.txt"
  [ "$v" = "0x00000001" ] || { echo "ABORT: $h is not on selective repeat (ROCE_ACCL=$v). Run: bash tools/cc_mode.sh sr"; exit 1; }
done
echo "retransmission: SR (ROCE_ACCL selective_repeat_forced_en=1 on $HOSTS)" | tee -a "$OUT/retrans_mode.txt"
# every node runs both planes (roles.sh all, 2026-09-04); who receives is
# decided by the flow table, not by the role assignment
bash tools/lab-infra/roles.sh all >/dev/null
sleep 4
# udp_blast too: a sink left from an earlier run keeps its port, the new
# sink's bind then fails and the client sees "Connection refused" (V7,
# 2026-09-08: 6.8 G of the 40 G background reached the receiver)
for h in $HOSTS; do on_host "$h" 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; pkill -x udp_blast 2>/dev/null; true'; done
sleep 1

snap_cnp() {
  for h in $SENDERS; do
    on_host "$h" 'for c in rp_cnp_handled rp_cnp_ignored; do printf "%s.mlx5_3.%s=%s\n" '"$h"' $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done'
  done
  for h in $RECVS; do
    on_host "$h" 'for c in np_cnp_sent np_ecn_marked_roce_packets; do printf "%s.mlx5_3.%s=%s\n" '"$h"' $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done'
  done
}
# swp37s0 / swp4s1 are the two side-B host ports (sgpu02 / sgpu04, the
# receivers of V8); swp21/swp25 are the two ends of the loopback cable that
# splits the switch in two (tools/lab-infra/switch/README.md) - the core link
# the receivers' ledgers cannot see. Unplugged, they just read zero.
# Egress queue counters, raw. The table view of "nv show ... counters qos"
# prints tx-uc-buffer-discards with a byte suffix although the counter is a
# FRAME count: a raw 76993 renders as "75.19 KB". Reading the table therefore
# understates loss by three orders of magnitude. Take the JSON.
snap_switch() { for p in swp37s0 swp4s1 swp21 swp25; do echo "## $p"; timeout 60 ssh -o BatchMode=yes sn5600 "nv show interface $p counters qos -o json 2>/dev/null" 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin).get('egress-queue-stats',{})
except Exception: sys.exit(0)
print('# tc tx-frames tx-bytes tx-uc-buffer-discards ecn-marked-frames wred-discards')
for tc in sorted(d, key=int):
    v=d[tc]
    print(tc, v.get('tx-frames',0), v.get('tx-bytes',0), v.get('tx-uc-buffer-discards',0), v.get('ecn-marked-frames',0), v.get('wred-discards',0))
"; done; }
snap_cnp > "$OUT/cnp_pre.txt"; snap_switch > "$OUT/switch_pre.txt"
# the core link (tools/lab-infra/switch/): 200G or 400G
bash tools/lab-infra/switch/split_core_speed.sh status > "$OUT/core_speed.txt" 2>/dev/null || true

# ---- T0, listeners, sampler, launches -------------------------------------
# TCP rows use iperf3's --start-at, the local patch built from
# ~/hyperfront/iperf320 and installed on all four hosts (paper/1-1_20260826/
# perftest_start_at.md). It is NOT listed in --help - the patch adds the option
# and its parsing, not the help text - so check for it the way that works.
for h in $RECVS $(echo "$ROWS" | awk '$5=="tcp"{print $1}' | sort -u); do
  on_host "$h" "strings /usr/local/lib/libiperf.so.0 2>/dev/null | grep -q start-at" \
    || { echo "ABORT: $h has an iperf3 without the --start-at patch"; exit 1; }
done
if echo "$ROWS" | awk '{print $5}' | grep -q '^udp$'; then
  for h in $RECVS $(echo "$ROWS" | awk '$5=="udp"{print $1}' | sort -u); do
    on_host "$h" "[ -x /tmp/udp_blast ] || gcc -O2 -pthread -o /tmp/udp_blast $REPO/tools/host/udp_blast.c" || { echo "ABORT: udp_blast missing on $h"; exit 1; }
  done
fi
T0=$(python3 -c "import time;print(int(time.time())+20)")
echo "$T0" > "$OUT/t0.txt"

# ---- a hidden bottleneck at a VF (class=meter rows) ------------------------
# A class=meter row lowers the receiver-side OVS drop meter on one VF for a
# window while the registry keeps saying 50 G. The ledger never learns the
# path got narrower - it only sees the arrivals that survived. Excess is
# dropped, so the tenant CC sees loss; with the bucket that is the CC's to
# handle below R (design 6.4). No scenario of the current suite uses it; the
# facility stays for the day one does. The meter's own band byte counter
# stays 0 under hw-offload, so do not read drops from meter-stats; read them
# from the application logs or the rate gap.
METER_ROWS=$(echo "$ROWS" | awk '$5=="meter"')
meter_set() { # $1 = destination host, $2 = destination VF index, $3 = kbps
  # Changing the band rate alone does NOT reach a datapath flow that is
  # already installed - the rate binds when the megaflow is created, and
  # with hw-offload an established flow keeps the old one. Measured
  # 2026-09-02 on a live 48.9 G flow: mod-meter to 20 G left it at 48.9 G
  # for the whole ten seconds that followed. Reinstalling the three
  # OpenFlow rules invalidates the datapath entry and the new rate then
  # takes hold inside one 100 ms bucket, in both directions. The output is
  # explicit (the VF's representor), as in vf_caps.sh: the mesh overlay
  # never lets the bridge learn a local VF's MAC.
  local dip m rep; dip=$(ip_of "$1" "$2"); rep=$(rep_of "$1" "$2"); m=$((11+$2))
  ssh -n -o BatchMode=yes "$(dpu_of "$1")" "
    sudo ovs-ofctl -O OpenFlow13 mod-meter ovsbr-p1 'meter=$m,kbps,band=type=drop,rate=$3'
    sudo ovs-ofctl -O OpenFlow13 del-flows ovsbr-p1 'udp,nw_dst=$dip,tp_dst=4791'
    sudo ovs-ofctl -O OpenFlow13 del-flows ovsbr-p1 'tcp,nw_dst=$dip'
    sudo ovs-ofctl -O OpenFlow13 del-flows ovsbr-p1 'ip,nw_dst=$dip'
    sudo ovs-ofctl -O OpenFlow13 add-flow ovsbr-p1 'priority=122,udp,nw_dst=$dip,tp_dst=4791,actions=meter:$m,output:$rep'
    sudo ovs-ofctl -O OpenFlow13 add-flow ovsbr-p1 'priority=121,tcp,nw_dst=$dip,actions=meter:$m,output:$rep'
    sudo ovs-ofctl -O OpenFlow13 add-flow ovsbr-p1 'priority=120,ip,nw_dst=$dip,actions=meter:$m,output:$rep'" </dev/null
}
# ---- the arm on every sender's executor (design 6: the bucket) -------------
# HPFT_RDMA_CC_ALGO: the tenant CC the RDMA executor runs (0xccd): 2 = DCQCN
#   (default), 3 = Swift. A flow table whose rdma rows carry rdma_cc=swift
#   selects 3 by itself. Selecting one resets every QP's CC state on the
#   device, so it is written before any flow starts.
# HPFT_LAW: the executor's law (0xcce <n> 22): 0 = the bucket (default),
#   r_i = c_i min(1, R / sum c_j); 1 = equal cap min(c_i, R/N); 2 = equal
#   split R/N ignoring the CC. 1 and 2 are the ablation arms of design 6.
# HPFT_CC_ONLY=1: the tenant CC alone, HyperFront out of the picture: the
#   RDMA executor ignores its budgets (0xcce 1 12), the sender agents are
#   stopped so nothing writes TCP rates, and the TCP rate table is emptied
#   (an entry left from an earlier run keeps shaping - measured 14.7 G on a
#   "CC alone" arm, 2026-09-06). The receiver agents keep running, so the
#   ledger's view of the run is still recorded and distilled.
CCALGO=${HPFT_RDMA_CC_ALGO:-2}
echo "$ROWS" | awk '$5=="rdma" && $9 ~ /rdma_cc=swift/' | grep -q . && CCALGO=3
LAW=${HPFT_LAW:-0}
CCONLY=${HPFT_CC_ONLY:-0}
# HPFT_RP_KNOBS: extra executor knobs, semicolon-separated mailbox lines
#   (e.g. "0xcce 1 23;0xcce 5000 24"), written after the arm and restored to
#   the executor's defaults at exit only for the two the runner knows
#   (23 = per-event denominator, 24 = drawing window in us).
RPKNOBS=${HPFT_RP_KNOBS:-}
mbox() { # $1 host: write the remaining args, one line each, to its DPU's RP FIFO
  local d; d=$(dpu_of "$1"); shift
  local cmd=""; for m in "$@"; do cmd+="timeout 5 bash -c 'echo \"$m\" > /tmp/rp_fifo'; "; done
  ssh -n -o BatchMode=yes "$d" "$cmd true" </dev/null >/dev/null 2>&1 || true
}
cleanup() {
  [ -n "$METER_ROWS" ] && echo "$METER_ROWS" | while read -r _ _ dh dv _; do meter_set "$dh" "$dv" 50000000 >/dev/null 2>&1; done
  for h in $SENDERS; do mbox "$h" "0xccd 2" "0xcce 0 22" "0xcce 0 12"; done
  [ -z "$RPKNOBS" ] || for h in $SENDERS; do mbox "$h" "0xcce 1 23" "0xcce 1000 24"; done
  [ "$CCONLY" = 0 ] || bash tools/lab-infra/roles.sh all >/dev/null 2>&1
  return 0
}
trap cleanup EXIT
for h in $SENDERS; do mbox "$h" "0xccd $CCALGO" "0xcce $LAW 22" "0xcce $CCONLY 12"; done
if [ -n "$RPKNOBS" ]; then
  IFS=';' read -r -a KN <<<"$RPKNOBS"
  for h in $SENDERS; do mbox "$h" "${KN[@]}"; done
fi
if [ "$CCONLY" = 1 ]; then
  for h in $SENDERS; do
    ssh -n -o BatchMode=yes "$(dpu_of $h)" "sudo systemctl stop hpft-txagent-e 2>/dev/null; sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; true" </dev/null >/dev/null 2>&1
    on_host "$h" 'B=$(ls -1 /usr/lib/linux-tools-*/bpftool 2>/dev/null | sort -V | tail -1); B=${B:-bpftool}; M=/sys/fs/bpf/hpft_tcp_edt/maps/hpft_pair_cfg; sudo $B map dump pinned $M -j 2>/dev/null | python3 -c "
import json,sys,subprocess
B=sys.argv[1]; M=sys.argv[2]
try: d=json.load(sys.stdin)
except Exception: d=[]
n=0
for e in d:
    k=e.get(\"key\") or e.get(\"formatted\",{}).get(\"key\")
    if isinstance(k,list): kh=[\"%02x\"%int(x,16) if isinstance(x,str) else \"%02x\"%x for x in k]
    else: continue
    subprocess.run([\"sudo\",B,\"map\",\"delete\",\"pinned\",M,\"key\",\"hex\"]+kh,capture_output=True); n+=1
print(\"tcp rate table cleared: %d entries\"%n)
" "$B" "$M"' 2>/dev/null | sed "s/^/  $h /"
  done
  sleep 2
fi
echo "rdma executor: cc algo = $CCALGO (2 DCQCN, 3 Swift); law = $LAW (0 bucket, 1 equal cap, 2 equal split); cc-only arm = $CCONLY; knobs = ${RPKNOBS:-default}" | tee "$OUT/arm.txt"
if [ -n "$METER_ROWS" ]; then
  : > "$OUT/hidden_meter.txt"
  while read -r _ _ dh dv _ _ st en opt; do
    g=${opt#gbps=}; at=$((T0+WARM+st))
    echo "$dh/vf$dv -> $g G from ${st}s to ${en}s (registry unchanged at 50 G)" | tee -a "$OUT/hidden_meter.txt"
    ( d=$((at - $(date +%s))); [ "$d" -gt 0 ] && sleep "$d"
      meter_set "$dh" "$dv" $((g*1000000)); sleep $((en-st)); meter_set "$dh" "$dv" 50000000 ) 9>&- >/dev/null 2>&1 &
  done <<<"$METER_ROWS"
fi
# executor sampler for the whole run: every sender DPU's RDMA executor is
# read once a second per flow set (0xded: R, the sum of the paced rates, the
# sum of the CC rates, the live sum, the QP counts). Each query holds the
# mailbox 13-22 ms, so it is a small perturbation of the budget push, not a
# passive tap; HPFT_NO_RP_SAMPLE=1 runs without it.
SAMP=$((END+WARM+30))
if [ -z "${HPFT_NO_RP_SAMPLE:-}" ]; then
  for h in $SENDERS; do
    d=$(dpu_of $h)
    scp -q "$REPO/tools/dpu/rp_sample.py" "$d:/tmp/rp_sample.py"
    ssh -n -o BatchMode=yes "$d" "sudo rm -f /tmp/rp_sample.jsonl /tmp/rp_sample.err; setsid nohup python3 /tmp/rp_sample.py $SAMP /tmp/rp_sample.jsonl 1 200 >/tmp/rp_sample.err 2>&1 </dev/null &" </dev/null
  done
fi
# listeners, on each row's destination host
declare -A SRV
k=0
while read -r sh sv dh dv cls n st en opt; do
  case "$cls" in meter|core) k=$((k+1)); continue ;; esac
  dur=$((en-st)); off=$((WARM+st)); [ "$st" -eq 0 ] && { dur=$((en+WARM)); off=0; }
  if [ "$cls" = rdma ]; then
    SRV[$dh]+="setsid nohup $PT -d $(dev_of $dh $dv) -q $n -m $QMTU $(rl_size "$opt") -p $((27000+k)) --report_gbits -D $dur --start_at=$((T0+off)) >/tmp/val_s$k.log 2>&1 </dev/null & "
  elif [ "$cls" = udp ]; then
    SRV[$dh]+="setsid nohup /tmp/udp_blast -r -p $((5900+k)) -B $(ip_of $dh $dv) >/tmp/val_s$k.log 2>&1 </dev/null & "
  else
    SRV[$dh]+="setsid nohup iperf3 -s -p $((5600+k)) >/tmp/val_s$k.log 2>&1 </dev/null & "
  fi
  k=$((k+1))
done <<<"$ROWS"
for h in $RECVS; do on_host "$h" "${SRV[$h]} true"; done
sleep 2
# one vport-meter sampler per receiving DPU (the meter runs on every DPU)
for h in $RECVS; do
  d=$(dpu_of $h)
  scp -q "$REPO/tools/dpu/vpm_sample.py" "$d:/tmp/vpm_sample.py"
  timeout 15 ssh -n -o BatchMode=yes "$d" "rm -f /tmp/vpm_series.csv; nohup python3 /tmp/vpm_sample.py $((END+WARM+40)) /tmp/vpm_series.csv 100 </dev/null >/dev/null 2>&1 & true" || echo "WARN: vpm sampler did not start on $d"
done
declare -A CLI
k=0
while read -r sh sv dh dv cls n st en opt; do
  case "$cls" in meter|core) k=$((k+1)); continue ;; esac
  dur=$((en-st)); off=$((WARM+st)); [ "$st" -eq 0 ] && { dur=$((en+WARM)); off=0; }
  dip=$(ip_of $dh $dv); sip=$(ip_of $sh $sv)
  # An RDMA row's perftest gets the destination's MANAGEMENT name, not its VF
  # IP. perftest opens a TCP control connection to that address to exchange QP
  # information; the data path is chosen by the two -d devices and their GIDs,
  # so it stays VF to VF either way (checked 2026-09-04: 45.5 G and every byte
  # through the destination representor). Sent to the VF IP, that idle control
  # connection is a TCP flow-set at the receiver for the whole run - it carries
  # a couple of kilobytes at setup and nothing after - and the sender's uplink
  # tree then reserves a class share for it: on a VF pair carrying RDMA and no
  # TCP, the RDMA flow-set's share halves (its tree fell from 50 to 36 G,
  # measured V8c 2026-09-04) and the scenario silently loses a third of its
  # intended load. The management network (192.168.1.0/24, ens37f0) is
  # reachable by name from all four hosts and carries no experiment traffic.
  rctl=$dh
  extra=""
  case "$opt" in
    # perftest's SW rate limiter (the HW one is refused - the PCC executor
    # owns the QP's rate) sends burst_size messages then busy-waits out the
    # gap, and burst_size defaults to tx_depth = 128 messages of 64 KB. One
    # burst per 6.7 ms means a 20 ms telemetry sample holds 3 of them and
    # quantises to +-33%, which is why an application-limited flow reads as
    # ~18% jitter while its 100 ms wire rate is smooth to 0.9%. Do NOT
    # shrink burst_size to fix that: at 8 the flow could not fill a fence
    # sitting at its 2 G floor, so the floor never lifted (the cap is twice
    # the flow's OWN use) and the fence stayed there for the whole phase -
    # measured r23, the phase delivered 0.26 G of a 10 G demand.
    rate_limit=*) extra="--rate_limit=${opt#rate_limit=}" ;;
    tcp_cc=*)     extra="-C ${opt#tcp_cc=}" ;;
    gbps=*)       extra="${opt#gbps=}" ;;
  esac
  if [ "$cls" = rdma ]; then
    # A late RDMA row is launched 5 s before its start (perftest's setup -
    # connect, get_cpu_mhz - takes well under a second), for the same
    # reason as the TCP rows below: its idle control connection would
    # otherwise count as a live TCP flow-set at the receiver for the whole
    # wait (V9, 2026-09-02).
    if [ "$st" -gt 0 ]; then
      cmd="setsid nohup bash -c 'python3 -c \"import time;time.sleep(max(0,$T0+$off-5-time.time()))\"; $PT -d $(dev_of $sh $sv) -q $n -m $QMTU $(rl_size "$opt") -p $((27000+k)) --report_gbits -D $dur --start_at=$((T0+off)) $extra $rctl' >/tmp/val_c$k.log 2>&1 </dev/null & "
    else
      cmd="setsid nohup $PT -d $(dev_of $sh $sv) -q $n -m $QMTU $(rl_size "$opt") -p $((27000+k)) --report_gbits -D $dur --start_at=$((T0+off)) $extra $rctl >/tmp/val_c$k.log 2>&1 </dev/null & "
    fi
  elif [ "$cls" = udp ]; then
    cmd="setsid nohup bash -c 'python3 -c \"import time;time.sleep(max(0,$T0+$off-time.time()))\"; /tmp/udp_blast -c $dip -p $((5900+k)) -B $sip -G $extra -t $dur' >/tmp/val_c$k.log 2>&1 </dev/null & "
  else
    # A late TCP row is launched 3 s before its start, not at T0: iperf3
    # opens its control connection at launch, and a control connection that
    # then idles for tens of seconds was closed by the far end a few seconds
    # into the data phase (V2 one-class, 2026-09-03: "control socket has
    # closed unexpectedly" at +4.5 s after a 45 s idle). The receiver also
    # sees that idle connection as a live TCP flow-set and halves the VM's
    # class split around it (V9, 2026-09-02). 3 s covers iperf3's setup.
    if [ "$st" -gt 0 ]; then
      cmd="setsid nohup bash -c 'python3 -c \"import time;time.sleep(max(0,$T0+$off-3-time.time()))\"; iperf3 -B $sip%dpu1vf$sv -c $dip -p $((5600+k)) -P $n -t $dur --start-at $((T0+off)) -J $extra' >/tmp/val_c$k.log 2>&1 </dev/null & "
    else
      cmd="setsid nohup iperf3 -B $sip%dpu1vf$sv -c $dip -p $((5600+k)) -P $n -t $dur --start-at $((T0+off)) -J $extra >/tmp/val_c$k.log 2>&1 </dev/null & "
    fi
  fi
  CLI[$sh]+="$cmd"
  k=$((k+1))
done <<<"$ROWS"
for h in $SENDERS; do on_host "$h" "${CLI[$h]} true"; done
LAUNCHED=$(date +%s); echo "$LAUNCHED" > "$OUT/launched.txt"
[ "$LAUNCHED" -lt "$T0" ] || echo "WARNING: launched $((LAUNCHED-T0)) s AFTER T0 - the t=0 rows started late"
sleep $((T0 - LAUNCHED + WARM + END + 6))

# ---- collect ------------------------------------------------------------------
for h in $SENDERS; do
  scp -q "$(dpu_of $h):/tmp/rp_sample.jsonl" "$OUT/rp_$h.jsonl" 2>/dev/null || true
done
snap_cnp > "$OUT/cnp_post.txt"
for h in $RECVS; do scp -q "$(dpu_of $h):/tmp/hpft_rxagent_e.jsonl" "$OUT/rx_$h.jsonl"; done
for h in $SENDERS; do scp -q "$(dpu_of $h):/tmp/hpft_txagent_e.jsonl" "$OUT/tx_$h.jsonl" 2>/dev/null || true; done
k=0
while read -r sh sv dh dv cls n st en opt; do
  case "$cls" in meter|core) k=$((k+1)); continue ;; esac
  if [ "$sh" = "$(hostname)" ]; then cp /tmp/val_c$k.log "$OUT/flow${k}_${sh}vf${sv}_to_${dh}vf${dv}_${cls}.log"; else scp -q "$sh:/tmp/val_c$k.log" "$OUT/flow${k}_${sh}vf${sv}_to_${dh}vf${dv}_${cls}.log"; fi
  k=$((k+1))
done <<<"$ROWS"
sleep 8
for h in $RECVS; do scp -q "$(dpu_of $h):/tmp/vpm_series.csv" "$OUT/vpm_series_$h.csv"; done
for h in $HOSTS; do on_host "$h" 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; pkill -x udp_blast 2>/dev/null; true'; done
# 9>&-: this child must NOT inherit the run lock. Without it the lock
# stays held for 40 s after run.sh exits and the next run aborts.
( sleep 40; snap_switch > "$OUT/switch_post.txt" ) 9>&- &
echo "$TAG done -> $OUT  (switch counters land in ~40 s)"
