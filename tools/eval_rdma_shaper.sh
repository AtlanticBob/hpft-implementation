#!/usr/bin/env bash
# RDMA-shaper eval, mirroring eval_tcp_shaper.sh's 4 metrics + fairness.
#   R-M1 cap goodput   : ib_write_bw @cap, 1 QP           -> shaping accuracy
#   R-M2 idle latency  : ib_write_lat 2B, pair idle        -> small-msg lat idle
#   R-M3 sat  latency  : ib_write_lat 2B while bw fills cap -> the key metric
#   R-M4 sat  goodput  : ib_write_bw goodput during M3      -> throughput kept
# Cap enforced via DOCA PCC on DPA; cap set through the receiver caps file the
# rx_agent hot-reads (units = rate_bps/200e9 * 2^20). vf0 = mlx5_6 -> 10.1.0.2.
set -u
PT=/home/zhaoxiang/hyperfront/perftest-26015
LDEV=mlx5_6; RDEV=mlx5_6; PEER=10.1.0.2; DSTIP=10.1.0.2
CAP_G=${1:-8}
LABEL="${2:-rdma}"
CAPS=/tmp/hpft_caps.conf

units() { python3 -c "print(round($1e9/200e9*(1<<20)))"; }
set_vf0_cap() {  # $1 = Gbit; rewrite caps file keeping other pairs
    local u; u=$(units "$1")
    ssh -o BatchMode=yes "$RDPU" "awk -v u=$u '{if(\$1==\"$DSTIP\")print \$1,u; else print}' $CAPS > /tmp/c.$$ && mv /tmp/c.$$ $CAPS"
    sleep 1.2   # let rx_agent -> tx_agent -> mailbox -> water-level settle
}
bw_srv() { ssh -o BatchMode=yes "$RECV" "pkill -9 -x ib_write_bw 2>/dev/null; sleep 0.3; setsid nohup $PT/ib_write_bw -F -d $RDEV -p $1 -s 65536 -q ${2:-1} --report_gbits -D ${3:-8} >/tmp/rbw_s.log 2>&1 </dev/null & sleep 1.3"; }
lat_srv() { ssh -o BatchMode=yes "$RECV" "pkill -9 -x ib_write_lat 2>/dev/null; sleep 0.3; setsid nohup $PT/ib_write_lat -F -d $RDEV -p $1 -s 2 -n ${2:-3000} >/tmp/rlat_s.log 2>&1 </dev/null & sleep 1.2"; }
bw_val() { grep -E "^ 65536" "$1" | awk '{print $4}' | tail -1; }
lat_val() { grep -E "^ 2 " "$1" | awk '{printf "typ=%s p99=%s p999=%s",$5,$8,$9}'; }

set_vf0_cap "$CAP_G"

# R-M1: cap goodput (1 QP bulk)
bw_srv 18720 1 8
timeout 20 $PT/ib_write_bw -F -d $LDEV -p 18720 -s 65536 -q 1 --report_gbits -D 8 $PEER >/tmp/rm1.log 2>&1
M1=$(bw_val /tmp/rm1.log)

# R-M2: idle latency
lat_srv 18721 3000
timeout 25 $PT/ib_write_lat -F -d $LDEV -p 18721 -s 2 -n 3000 $PEER >/tmp/rm2.log 2>&1
M2=$(lat_val /tmp/rm2.log)

# R-M3 + R-M4: saturated latency + goodput (concurrent, same pair)
# The receiver follows the registry: these micro-benchmarks are two-node by
# nature, but which two depends on the run's role assignment.
RECV=${RECV:-$(python3 -c "import json;print(json.load(open('/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-registry.json'))['receiver_host'])")}
RDPU=${RDPU:-$(python3 -c "import json;r=json.load(open('/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-registry.json'));print({n['host']:n['dpu'] for n in r['nodes']}[r['receiver_host']])")}
bw_srv 18722 1 14                 # long bulk to saturate the pair
( timeout 30 $PT/ib_write_bw -F -d $LDEV -p 18722 -s 65536 -q 1 --report_gbits -D 14 $PEER >/tmp/rm4.log 2>&1 & )
sleep 3
lat_srv 18723 3000
timeout 25 $PT/ib_write_lat -F -d $LDEV -p 18723 -s 2 -n 3000 $PEER >/tmp/rm3.log 2>&1
M3=$(lat_val /tmp/rm3.log)
for _ in $(seq 1 15); do grep -qE "^ 65536" /tmp/rm4.log 2>/dev/null && break; sleep 1; done
M4=$(bw_val /tmp/rm4.log)
ssh -o BatchMode=yes "$RECV" 'pkill -9 -x ib_write_bw 2>/dev/null; pkill -9 -x ib_write_lat 2>/dev/null'

printf "%-14s cap=%sG | R-M1 acc=%-5sG | R-M2 idle[%s] | R-M3 SAT[%s] | R-M4 sat-gp=%sG\n" \
  "$LABEL" "$CAP_G" "${M1:-?}" "${M2:-?}" "${M3:-?}" "${M4:-?}"
