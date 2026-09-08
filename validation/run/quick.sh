#!/usr/bin/env bash
# Quick RDMA/TCP experiment runner for the per-QP executor (2026-09-06).
#
# Everything the validation suite does is here in miniature: set the arm on
# every sender's executor, start the flows at the same moment, wait, and
# print one line per flow plus the aggregate. Runs are meant to be short
# (<= 30 s) so a design question can be answered in one sitting.
#
# usage: quick.sh <name> <secs> <spec-file>
#   spec lines:  rdma <srchost> <srcvf> <dsthost> <dstvf> <qps>
#                tcp  <srchost> <srcvf> <dsthost> <dstvf> <streams>
#                udp  <srchost> <srcvf> <dsthost> <dstvf> <gbps>   (udp_blast: background
#                     traffic HyperFront neither schedules nor shapes, ip_other at the receiver)
# env:
#   ALGO=2|3        tenant RDMA CC (2 DCQCN, 3 Swift), default 2
#   CC_ONLY=0|1     run the tenant CC alone, HyperFront ignored
#   LAW=0|1|2       0 bucket (default): r_i = c_i min(1, R/sum c_j)
#                   1 equal cap: r_i = min(c_i, R/N)   2 equal split ignoring the CC: R/N
#   AGENTS=0|1      keep the HyperFront agents running (default 1)
#   TCP_CC=cubic    TCP congestion control
#   RDMA_MTU=1024   perftest -m (4096 needs VF and representor MTU >= 4200)
#   RP_BIN=<path>   run another PCC binary on the sender DPUs (diagnostics only; knobs are ignored)
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
NAME=${1:?name}; DUR=${2:?secs}; SPEC=${3:?spec file}
ALGO=${ALGO:-2}; CC_ONLY=${CC_ONLY:-0}; LAW=${LAW:-0}
AGENTS=${AGENTS:-1}; TCP_CC=${TCP_CC:-cubic}; RDMA_MTU=${RDMA_MTU:-1024}
# CC alone means no HyperFront at all: the RDMA executor is told to ignore
# its budgets, the sender agents are stopped so nothing keeps writing TCP
# rates, and the TCP rate table is emptied (an entry left from a previous
# run keeps shaping - measured 14.7 G on a "CC alone" arm, 2026-09-06).
[ "$CC_ONLY" = 1 ] && AGENTS=0
SW_TARGET=${SW_TARGET:-25000}; SW_RATE_SRTT=${SW_RATE_SRTT:-2}; SW_MDF=${SW_MDF:-32768}; SW_BETA=${SW_BETA:-52429}; SW_FS=${SW_FS:-40000}
PT=$HOME/hyperfront/perftest-enhanced/ib_write_bw
OUT=/tmp/quick_$NAME; rm -rf "$OUT"; mkdir -p "$OUT"

LOCAL=$(hostname)
on_host() { # $1 host, rest command -- ssh unless it is this machine
  local h=$1; shift
  if [ "$h" = "$LOCAL" ]; then bash -c "$*"; else ssh -n -o BatchMode=yes "$h" "$*"; fi
}
reg() { python3 -c "import json,sys;r=json.load(open('config/lab-registry.json'));print($1)"; }
dev_of() { reg "next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }
ip_of()  { reg "next(v['ip'] for v in r['vnics'] if v['host']=='$1' and v['netdev']=='dpu1vf$2')"; }
dpu_of() { reg "{n['host']:n['dpu'] for n in r['nodes']}['$1']"; }

HOSTS=$(awk '{print $2; print $4}' "$SPEC" | sort -u)
SENDERS=$(awk '{print $2}' "$SPEC" | sort -u)

# ---- arm every sender's executor -------------------------------------
for h in $SENDERS; do
  d=$(dpu_of "$h")
  if [ "$AGENTS" = 0 ]; then
    ssh -n -o BatchMode=yes "$d" "sudo systemctl stop hpft-txagent-e 2>/dev/null" >/dev/null 2>&1
  fi
  ssh -n -o BatchMode=yes "$d" "RP_BIN=${RP_BIN:-} bash /opt/hpft/rp_service.sh start >/dev/null 2>&1" >/dev/null 2>&1
done
sleep 2
for h in $SENDERS; do
  d=$(dpu_of "$h")
  for m in "0xccd $ALGO" "0xcce $CC_ONLY 12" "0xcce $LAW 22" "0xcd1 ${SW_TARGET:-6000} 0" "0xcd1 $SW_FS 1" "0xcd1 $SW_BETA 5" "0xcd1 $SW_MDF 6" "0xcd1 ${SW_AI:-256} 4" "0xcd1 ${SW_ALPHA:-50000} 2" "0xcd1 ${SW_BETA_NS:-5000} 3" "0xcd1 $SW_RATE_SRTT 8" "0xcce ${SW_CTX:-1} 25"; do
    ssh -n -o BatchMode=yes "$d" "timeout 5 bash -c 'echo \"$m\" > /tmp/rp_fifo'" >/dev/null 2>&1
  done
done
if [ "$AGENTS" != 0 ]; then
  for h in $SENDERS; do
    d=$(dpu_of "$h")
    ssh -n -o BatchMode=yes "$d" "systemctl is-active hpft-txagent-e >/dev/null 2>&1 || { sudo systemctl reset-failed hpft-txagent-e 2>/dev/null; sudo systemd-run --unit hpft-txagent-e /usr/bin/python3 /opt/hpft/tx_agent_e.py --local-host $h; }" >/dev/null 2>&1
  done
fi
if [ "$CC_ONLY" = 1 ]; then
  for h in $SENDERS; do
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
fi
echo "arm: algo=$ALGO cc_only=$CC_ONLY law=$LAW agents=$AGENTS tcp_cc=$TCP_CC" | tee "$OUT/arm.txt"
for h in $SENDERS; do
  d=$(dpu_of "$h")
  v=$(ssh -n -o BatchMode=yes "$d" "N=\$(wc -l < /tmp/pcc_rp.log); timeout 5 bash -c 'echo \"0xdef 0\" > /tmp/rp_fifo'; sleep 1; tail -n +\$((N+1)) /tmp/pcc_rp.log | grep -a HPFT_RSP | tail -1 | sed 's/.*evb32=//'" 2>/dev/null)
  echo "  $h readback algo/cc_only = $v" | tee -a "$OUT/arm.txt"
done

# ---- clear anything a previous run left listening ---------------------
# iperf3 -s never exits, and a stale one holding port 28000+k stops the
# perftest server for that index from binding: the flow then reports
# nothing and its share silently lands on its neighbours (2026-09-07).
for h in $HOSTS; do
  on_host "$h" "pkill -x iperf3 2>/dev/null; pkill -x ib_write_bw 2>/dev/null; pkill -x udp_blast 2>/dev/null; true" >/dev/null 2>&1
done
if awk '{print $1}' "$SPEC" | grep -q '^udp$'; then
  for h in $HOSTS; do
    on_host "$h" "[ -x /tmp/udp_blast ] || gcc -O2 -pthread -o /tmp/udp_blast $REPO/tools/host/udp_blast.c" >/dev/null 2>&1
  done
fi
sleep 1

# ---- servers ---------------------------------------------------------
k=0
declare -A SRV
while read -r cls sh sv dh dv n; do
  [ -n "${cls:-}" ] || continue
  case "$cls" in \#*) continue ;; esac
  p=$((28000 + k))
  if [ "$cls" = rdma ]; then
    SRV[$dh]+="setsid nohup $PT -d $(dev_of $dh $dv) -q $n -m $RDMA_MTU -p $p --report_gbits -D $DUR >/tmp/qs$k.log 2>&1 </dev/null & "
  elif [ "$cls" = udp ]; then
    SRV[$dh]+="setsid nohup /tmp/udp_blast -r -p $p -B $(ip_of $dh $dv) >/tmp/qs$k.log 2>&1 </dev/null & "
  else
    SRV[$dh]+="setsid nohup iperf3 -s -p $p >/tmp/qs$k.log 2>&1 </dev/null & "
  fi
  k=$((k+1))
done < "$SPEC"
for h in "${!SRV[@]}"; do on_host "$h" "${SRV[$h]} true" >/dev/null 2>&1; done
sleep 2

# ---- clients, all at one absolute instant ----------------------------
T0=$(( $(date +%s) + 6 ))
echo "$T0" > "$OUT/t0.txt"      # probes wait for this instant
k=0
declare -A CLI
while read -r cls sh sv dh dv n; do
  [ -n "${cls:-}" ] || continue
  case "$cls" in \#*) continue ;; esac
  p=$((28000 + k))
  if [ "$cls" = rdma ]; then
    CLI[$sh]+="setsid nohup $PT -d $(dev_of $sh $sv) -q $n -m $RDMA_MTU -p $p --report_gbits -D $DUR --start_at=$T0 $dh >/tmp/qc$k.log 2>&1 </dev/null & "
  elif [ "$cls" = udp ]; then
    CLI[$sh]+="setsid nohup bash -c 'python3 -c \"import time;time.sleep(max(0,$T0-time.time()))\"; /tmp/udp_blast -c $(ip_of $dh $dv) -p $p -B $(ip_of $sh $sv) -G $n -t $DUR' >/tmp/qc$k.log 2>&1 </dev/null & "
  else
    CLI[$sh]+="setsid nohup iperf3 -c $(ip_of $dh $dv) -p $p -P $n -t $DUR -C $TCP_CC --start-at $T0 -J -B $(ip_of $sh $sv) >/tmp/qc$k.log 2>&1 </dev/null & "
  fi
  k=$((k+1))
done < "$SPEC"
for h in "${!CLI[@]}"; do on_host "$h" "${CLI[$h]} true" >/dev/null 2>&1; done

sleep $((DUR + 12))

# ---- collect ---------------------------------------------------------
k=0; tot=0
printf "%-4s %-6s %-22s %8s\n" "#" "class" "flow" "Gb/s" | tee "$OUT/result.txt"
while read -r cls sh sv dh dv n; do
  [ -n "${cls:-}" ] || continue
  case "$cls" in \#*) continue ;; esac
  if [ "$cls" = rdma ]; then
    bw=$(on_host "$sh" "grep -a -A1 '#bytes' /tmp/qc$k.log | tail -1 | awk '{print \$4}'" 2>/dev/null)
  elif [ "$cls" = udp ]; then
    bw=$n    # requested background rate; it is not a tenant flow and is left out of TOTAL
    printf "%-4s %-6s %-22s %8s (background, not counted)\n" "$k" "$cls" "$sh/vf$sv>$dh/vf$dv" "$bw" | tee -a "$OUT/result.txt"
    k=$((k+1)); continue
  else
    # goodput = bytes the receiver got over the CLIENT's test duration. The
    # server's own bits_per_second divides by an interval that includes
    # the --start-at wait (17 s for a 12 s test here), which understated
    # every TCP number by up to a third and read as unfairness (2026-09-08).
    bw=$(on_host "$sh" "python3 -c \"
import json,sys
try:
    d=json.load(open('/tmp/qc$k.log')); e=d['end']
    print('%.2f' % (e['sum_received']['bytes']*8/e['sum_sent']['seconds']/1e9))
except Exception: print('')\"" 2>/dev/null)
  fi
  bw=${bw:-0}
  printf "%-4s %-6s %-22s %8s\n" "$k" "$cls" "$sh/vf$sv>$dh/vf$dv" "$bw" | tee -a "$OUT/result.txt"
  tot=$(python3 -c "print(round($tot + ${bw:-0}, 2))")
  k=$((k+1))
done < "$SPEC"
echo "TOTAL $tot Gb/s" | tee -a "$OUT/result.txt"
