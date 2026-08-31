#!/usr/bin/env bash
# validation runner: one scenario, one tag.
#
#   run.sh <scenario> <tag>        scenario = a file name in ../scenarios (without .flows)
#                                  -> results/<tag>/
#
# What it does, in order: refuses to run unless the lab matches the repo
# (deploy_check) and the standing registry carries the standard policy;
# assigns the planes (receiver sgpu02, senders = the hosts in the flow
# table); starts one listener per flow-table row on the receiver; starts
# the receiver's vport-meter sampler; launches every row from its own host
# so that its first packet lands at T0 + warm-up + start (RDMA: perftest
# --start_at on both ends; TCP: sleep until the slot); collects the three
# data paths (vport-meter series, per-flow application logs, agent jsonl)
# plus the trust/CNP/switch snapshots; restores nothing because it changed
# nothing standing.
#
# Time base: T0 is the absolute second the t=0 rows begin; the experiment
# clock (the one every table and figure uses) is T0 + WARM. A row with
# start s begins at T0 + WARM + s and runs until T0 + WARM + end.
set -u
DIR=$(cd "$(dirname "$0")" && pwd); VAL=$(cd "$DIR/.." && pwd); REPO=$(cd "$VAL/.." && pwd)
SCN=${1:?scenario}; TAG=${2:?tag}
FLOWS="$VAL/scenarios/$SCN.flows"; [ -f "$FLOWS" ] || { echo "no such scenario: $FLOWS"; exit 1; }
OUT="$VAL/results/$TAG"; mkdir -p "$OUT"
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
RECV=sgpu02; RDPU=hpft-dpu2; WARM=5; QMTU=1024
cd "$REPO"

reg() { python3 -c "import json,sys;r=json.load(open('config/lab-registry.json'));print($1)"; }
dev_of() { reg "next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }
ip_of()  { reg "next(v['ip'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }
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
SENDERS=$(echo "$ROWS" | awk '{print $1}' | sort -u | tr '\n' ' ')
END=$(echo "$ROWS" | awk 'BEGIN{m=0}{if($7>m)m=$7}END{print m}')
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
cp config/lab-registry.json "$OUT/registry.json"
for h in $RECV $SENDERS; do
  d=$(dpu_of $h); cur=$(ssh -o BatchMode=yes "$d" 'cat /sys/class/net/p1/speed' 2>/dev/null)
  [ "$cur" = "200000" ] || { echo "ABORT: $d p1 is ${cur} Mb, expected 200000"; exit 1; }
done
# environment as it actually is (UPCC, SR, p1 speed, agents, switch ECN);
# the report copies these lines, and the SR column is checked by distill
bash tools/lab_env.sh status > "$OUT/env_status.txt" 2>&1
bash tools/lab-infra/roles.sh set --receiver "$RECV" --senders "$(echo $SENDERS | tr ' ' ',')" >/dev/null
sleep 4
for h in $RECV $SENDERS; do on_host "$h" 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; true'; done
sleep 1

snap_cnp() {
  for h in $SENDERS; do
    on_host "$h" 'for c in rp_cnp_handled rp_cnp_ignored; do printf "%s.mlx5_3.%s=%s\n" '"$h"' $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done'
  done
  on_host "$RECV" 'for c in np_cnp_sent np_ecn_marked_roce_packets; do printf "sgpu02.mlx5_3.%s=%s\n" $c "$(cat /sys/class/infiniband/mlx5_3/ports/1/hw_counters/$c 2>/dev/null)"; done'
}
snap_switch() { timeout 60 ssh -o BatchMode=yes sn5600 "nv show interface swp37s0 counters qos 2>/dev/null" 2>/dev/null | grep -v Welcome | sed -n '/Egress Queue/,/PFC/p' | grep -E "^\s+[0-9]"; }
snap_trust() { # TCP executor trust per pair on every sender host
  for h in $SENDERS; do
    on_host "$h" 'B=$(ls -1 /usr/lib/linux-tools-*/bpftool | sort -V | tail -1); sudo $B map dump pinned /sys/fs/bpf/hpft_tcp_edt/maps/hpft_pair_state -j 2>/dev/null' \
      | python3 -c "
import json,sys,struct
# struct hpft_pair_state, ONE definition shared with tcp_shaper_lib.pack_pair_state
# and tools/host/bpf_trust_sample.sh. A second hand-written copy of this
# format is how a field removal silently turned this snapshot into a
# struct.error and left every run since without its TCP trust reading.
FMT = '<IIQQQQQIIIIQQ'
try: d=json.load(sys.stdin)
except Exception: d=[]
for e in d:
    v=bytes(int(x,16) for x in e['value']); k=bytes(int(x,16) for x in e['key'])
    f=struct.unpack(FMT, v[:struct.calcsize(FMT)])
    print('$h', k.hex(), 'trust=%.4f'%(f[9]/65536), 'loss_ep=%d'%f[10], 'shaper_shot_ms=%d'%f[1], 'cc_sum=%d'%f[4])"
  done
}
snap_cnp > "$OUT/cnp_pre.txt"; snap_switch > "$OUT/switch_pre.txt"

# ---- T0, listeners, sampler, launches -------------------------------------
if echo "$ROWS" | awk '{print $4}' | grep -q '^udp$'; then
  for h in $RECV $(echo "$ROWS" | awk '$4=="udp"{print $1}' | sort -u); do
    on_host "$h" "[ -x /tmp/udp_blast ] || gcc -O2 -pthread -o /tmp/udp_blast $REPO/tools/host/udp_blast.c" || { echo "ABORT: udp_blast missing on $h"; exit 1; }
  done
fi
T0=$(python3 -c "import time;print(int(time.time())+20)")
echo "$T0" > "$OUT/t0.txt"
# executor-state samplers for the whole run: TCP trust from the BPF map on
# every sender host (100 ms), RDMA pair state from the device on every
# sender DPU (1 s, one mailbox query per active pair)
SAMP=$((END+WARM+30))
for h in $SENDERS; do
  d=$(dpu_of $h)
  on_host "$h" "setsid nohup bash $REPO/tools/host/bpf_trust_sample.sh $SAMP /tmp/val_trust.jsonl 0.1 >/dev/null 2>&1 </dev/null &"
  # HPFT_NO_RP_PROBE=1 runs without the device readback. It is a real
  # perturbation, not a passive sampler: each query occupies the RP mailbox
  # for 13-22 ms, and with one query per pair per second the RDMA budget
  # push is disturbed a few times a second. Use it to separate what the
  # executor does from what observing it does.
  if [ -z "${HPFT_NO_RP_PROBE:-}" ]; then
    scp -q "$REPO/tools/dpu/rp_probe.py" "$d:/tmp/rp_probe.py"
    ssh -n -o BatchMode=yes "$d" "sudo rm -f /tmp/rp_probe.jsonl /tmp/rp_probe.err; setsid nohup python3 /tmp/rp_probe.py $SAMP /tmp/rp_probe.jsonl 1 200 >/tmp/rp_probe.err 2>&1 </dev/null &" </dev/null
  fi
done
k=0; SRV=""
while read -r sh sv dv cls n st en opt; do
  dur=$((en-st)); off=$((WARM+st)); [ "$st" -eq 0 ] && { dur=$((en+WARM)); off=0; }
  if [ "$cls" = rdma ]; then
    SRV+="setsid nohup $PT -d $(dev_of $RECV $dv) -q $n -m $QMTU $(rl_size "$opt") -p $((27000+k)) --report_gbits -D $dur --start_at=$((T0+off)) >/tmp/val_s$k.log 2>&1 </dev/null & "
  elif [ "$cls" = udp ]; then
    SRV+="setsid nohup /tmp/udp_blast -r -p $((5900+k)) -B $(ip_of $RECV $dv) >/tmp/val_s$k.log 2>&1 </dev/null & "
  else
    SRV+="setsid nohup iperf3 -s -p $((5600+k)) >/tmp/val_s$k.log 2>&1 </dev/null & "
  fi
  k=$((k+1))
done <<<"$ROWS"
on_host "$RECV" "$SRV true"
sleep 2
timeout 15 ssh -n -o BatchMode=yes "$RDPU" "rm -f /tmp/vpm_series.csv; nohup python3 /tmp/vpm_sample.py $((END+WARM+40)) /tmp/vpm_series.csv 100 </dev/null >/dev/null 2>&1 & true" || echo "WARN: vpm sampler did not start"
declare -A CLI
k=0
while read -r sh sv dv cls n st en opt; do
  dur=$((en-st)); off=$((WARM+st)); [ "$st" -eq 0 ] && { dur=$((en+WARM)); off=0; }
  dip=$(ip_of $RECV $dv); sip=$(ip_of $sh $sv)
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
    cmd="setsid nohup $PT -d $(dev_of $sh $sv) -q $n -m $QMTU $(rl_size "$opt") -p $((27000+k)) --report_gbits -D $dur --start_at=$((T0+off)) $extra $dip >/tmp/val_c$k.log 2>&1 </dev/null & "
  elif [ "$cls" = udp ]; then
    cmd="setsid nohup bash -c 'python3 -c \"import time;time.sleep(max(0,$T0+$off-time.time()))\"; /tmp/udp_blast -c $dip -p $((5900+k)) -B $sip -G $extra -t $dur' >/tmp/val_c$k.log 2>&1 </dev/null & "
  else
    cmd="setsid nohup bash -c 'python3 -c \"import time;time.sleep(max(0,$T0+$off-time.time()))\"; iperf3 -B $sip%dpu1vf$sv -c $dip -p $((5600+k)) -P $n -t $dur -J $extra' >/tmp/val_c$k.log 2>&1 </dev/null & "
  fi
  CLI[$sh]+="$cmd"
  k=$((k+1))
done <<<"$ROWS"
for h in $SENDERS; do on_host "$h" "${CLI[$h]} true"; done
LAUNCHED=$(date +%s); echo "$LAUNCHED" > "$OUT/launched.txt"
[ "$LAUNCHED" -lt "$T0" ] || echo "WARNING: launched $((LAUNCHED-T0)) s AFTER T0 - the t=0 rows started late"
sleep $((T0 - LAUNCHED + WARM + END + 6))

# ---- collect ------------------------------------------------------------------
snap_trust > "$OUT/trust_post.txt"
for h in $SENDERS; do
  d=$(dpu_of $h)
  if [ "$h" = "$(hostname)" ]; then cp /tmp/val_trust.jsonl "$OUT/trust_$h.jsonl"; else scp -q "$h:/tmp/val_trust.jsonl" "$OUT/trust_$h.jsonl"; fi
  scp -q "$d:/tmp/rp_probe.jsonl" "$OUT/rp_$h.jsonl" 2>/dev/null || true
done
snap_cnp > "$OUT/cnp_post.txt"
scp -q "$RDPU:/tmp/hpft_rxagent_e.jsonl" "$OUT/rx.jsonl"
for h in $SENDERS; do scp -q "$(dpu_of $h):/tmp/hpft_txagent_e.jsonl" "$OUT/tx_$h.jsonl" 2>/dev/null || true; done
k=0
while read -r sh sv dv cls n st en opt; do
  if [ "$sh" = "$(hostname)" ]; then cp /tmp/val_c$k.log "$OUT/flow${k}_${sh}vf${sv}_to_vf${dv}_${cls}.log"; else scp -q "$sh:/tmp/val_c$k.log" "$OUT/flow${k}_${sh}vf${sv}_to_vf${dv}_${cls}.log"; fi
  k=$((k+1))
done <<<"$ROWS"
sleep 8
scp -q "$RDPU:/tmp/vpm_series.csv" "$OUT/vpm_series.csv"
for h in $RECV $SENDERS; do on_host "$h" 'pkill -f "ib_write_b[w]" 2>/dev/null; pkill -x iperf3 2>/dev/null; true'; done
# 9>&-: this child must NOT inherit the run lock. Without it the lock
# stays held for 40 s after run.sh exits and the next run aborts.
( sleep 40; snap_switch > "$OUT/switch_post.txt" ) 9>&- &
echo "$TAG done -> $OUT  (switch counters land in ~40 s)"
