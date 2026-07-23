#!/usr/bin/env bash
# Why does GBN get more RDMA bandwidth than SR at the receiver policer?
#
# Isolation probe: RDMA ONLY (no TCP competition) through the same 20G meter.
# If GBN > SR persists without TCP, the gap is pure retransmission mechanics,
# not a token race against TCP.
#   usage: probe_gbn_vs_sr.sh <tag> <nflows>
set -u
TAG=$1; N=${2:-16}
BASE=/home/zhaoxiang/hyperfront/hpft-v2/paper/motiv_rx_cap_20260716
DIR=$BASE/probe/$TAG
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
DUR=20
mkdir -p "$DIR"

snap_h() { for d in mlx5_6 mlx5_7 mlx5_8 mlx5_9; do
  printf "%s.xmit=%s\n" $d "$(cat /sys/class/infiniband/$d/ports/1/counters/port_xmit_data 2>/dev/null)"
  printf "%s.seq=%s\n" $d "$(cat /sys/class/infiniband/$d/ports/1/hw_counters/packet_seq_err 2>/dev/null)"
done; }
snap_p() { ssh sgpu02 'printf "rcv=%s\n" "$(cat /sys/class/infiniband/mlx5_6/ports/1/counters/port_rcv_data 2>/dev/null)"'; }

ssh sgpu02 'pkill -f "ib_write_b[w]" 2>/dev/null; true'
pkill -f "ib_write_b[w]" 2>/dev/null || true
sleep 1
ssh sgpu02 "for k in \$(seq 0 $((N-1))); do v=\$((k%4)); nohup $PT -d mlx5_6 -p \$((19100+k)) --report_gbits -D $DUR > /tmp/pr_\$k.log 2>&1 & done; true"
sleep 3
snap_h > "$DIR/h_pre"; snap_p > "$DIR/p_pre"
for k in $(seq 0 $((N-1))); do
  v=$((k%4))
  $PT -d mlx5_$((6+v)) -p $((19100+k)) --report_gbits -D $DUR 10.1.$v.2 > "$DIR/f$k.log" 2>&1 &
done
wait
snap_h > "$DIR/h_post"; snap_p > "$DIR/p_post"

python3 - "$DIR" "$N" "$DUR" <<'EOF'
import re,sys,glob
D,N,DUR=sys.argv[1],int(sys.argv[2]),float(sys.argv[3])
def kv(p):
    d={}
    for ln in open(p):
        if '=' in ln:
            k,v=ln.strip().split('=',1)
            try: d[k]=int(v)
            except: pass
    return d
good=0.0
for f in sorted(glob.glob(f"{D}/f*.log")):
    m=re.search(r"^\s*65536\s+\d+\s+([\d.]+)\s+([\d.]+)",open(f,errors='replace').read(),re.M)
    good+= float(m.group(2)) if m else 0
a,b=kv(f"{D}/h_pre"),kv(f"{D}/h_post")
pa,pb=kv(f"{D}/p_pre"),kv(f"{D}/p_post")
off=sum(b.get(f"mlx5_{x}.xmit",0)-a.get(f"mlx5_{x}.xmit",0) for x in range(6,10))*4*8/DUR/1e9
seq=sum(b.get(f"mlx5_{x}.seq",0)-a.get(f"mlx5_{x}.seq",0) for x in range(6,10))
dlv=(pb.get("rcv",0)-pa.get("rcv",0))*4*8/DUR/1e9
print(f"  goodput={good:.2f}G  offered={off:.2f}G  delivered={dlv:.2f}G  "
      f"drop={100*(1-dlv/off) if off else 0:.1f}%  NAK={seq}  "
      f"eff={100*good/off if off else 0:.0f}%")
EOF
