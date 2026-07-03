#!/usr/bin/env bash
# S5: per-flowtag shared-cap matrix. Requires S5 PCC running in rate-stdin
# mode on the DPU with FIFO at /tmp/hpft_rate_fifo.
# vf0 flowtag=0x74249a41, vf1 flowtag=0x11f4386b (from S3).
set -u
PT=/home/zhaoxiang/hyperfront/perftest-26015/ib_write_bw
OUT=${1:?usage: run_s5_shared_cap.sh <output_dir>}
mkdir -p "$OUT"

set_cap() { ssh -o BatchMode=yes hpft-dpu "echo '$1 $2' > /tmp/hpft_rate_fifo"; sleep 0.2; }

# run N parallel same-pair processes, print aggregate goodput
agg() {
    # $1 name, $2 ldev, $3 rdev, $4 peer, $5 nprocs, $6 qps, $7 baseport
    local name=$1 ldev=$2 rdev=$3 peer=$4 np=$5 qps=$6 bp=$7
    for i in $(seq 1 $np); do
        ssh -o BatchMode=yes sgpu02 "setsid nohup $PT -F -d $rdev -p $((bp+i)) -s 65536 -q $qps --report_gbits -D 8 >/tmp/s5s_$i.log 2>&1 </dev/null &"
    done
    sleep 1.5
    local pids=()
    for i in $(seq 1 $np); do
        timeout 40 $PT -F -d $ldev -p $((bp+i)) -s 65536 -q $qps --report_gbits -D 8 $peer >"$OUT/${name}_c$i.log" 2>&1 &
        pids+=($!)
    done
    wait "${pids[@]}" 2>/dev/null
    python3 - "$name" "$OUT" $np <<'PY'
import sys
name, out, np = sys.argv[1], sys.argv[2], int(sys.argv[3])
tot = 0.0
for i in range(1, np+1):
    for line in open(f"{out}/{name}_c{i}.log"):
        if line.startswith(" 65536"):
            tot += float(line.split()[3])
print(f"{name}: aggregate {tot:.2f} Gb/s")
PY
}

echo "== T0 fail-open (no caps): vf0 1proc 1qp =="
agg t0_failopen mlx5_6 mlx5_6 10.1.0.2 1 1 18600

echo "== set vf0 cap = 8G (41943) =="
set_cap 0x74249a41 41943

echo "== T1 vf0 1proc 1qp =="
agg t1_1p1q mlx5_6 mlx5_6 10.1.0.2 1 1 18610
echo "== T2 vf0 1proc 4qp =="
agg t2_1p4q mlx5_6 mlx5_6 10.1.0.2 1 4 18620
echo "== T3 vf0 2proc 1qp =="
agg t3_2p1q mlx5_6 mlx5_6 10.1.0.2 2 1 18630
echo "== T4 vf0 4proc 1qp =="
agg t4_4p1q mlx5_6 mlx5_6 10.1.0.2 4 1 18640

echo "== T5 isolation: vf0 capped 8G + vf1 uncapped, concurrent =="
agg t5_vf0 mlx5_6 mlx5_6 10.1.0.2 1 1 18650 &
P1=$!
agg t5_vf1 mlx5_7 mlx5_7 10.1.1.2 1 1 18660 &
P2=$!
wait $P1 $P2

echo "== set vf1 cap = 4G (20972); T6 both capped concurrent =="
set_cap 0x11f4386b 20972
agg t6_vf0 mlx5_6 mlx5_6 10.1.0.2 1 1 18670 &
P1=$!
agg t6_vf1 mlx5_7 mlx5_7 10.1.1.2 1 1 18680 &
P2=$!
wait $P1 $P2

echo "== T7 delete caps, verify fail-open =="
set_cap 0x74249a41 0
set_cap 0x11f4386b 0
agg t7_failopen mlx5_6 mlx5_6 10.1.0.2 1 1 18690
echo "S5 matrix done"
