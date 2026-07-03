#!/usr/bin/env bash
# S0 baseline: RDMA perftest sgpu01 -> sgpu02 over VF pairs, no PCC running.
# Usage: tools/run_s0_baseline.sh <output_dir>
set -u
PT=/home/zhaoxiang/hyperfront/perftest-26015
PEER=sgpu02
OUT=${1:?usage: run_s0_baseline.sh <output_dir>}
mkdir -p "$OUT"

run_case() {
    # $1 name, $2 binary, $3 local dev, $4 remote dev, $5 peer ip, $6 port, $7 common args, $8 client-only args
    local name=$1 bin=$2 ldev=$3 rdev=$4 pip=$5 port=$6 common=$7 conly=$8
    echo "== $name =="
    ssh -o BatchMode=yes $PEER "setsid nohup $PT/$bin -F -d $rdev -p $port $common \
        >/tmp/s0_${name}_server.log 2>&1 </dev/null & sleep 1; pgrep -f 'p $port' >/dev/null && echo server_up"
    timeout 60 $PT/$bin -F -d "$ldev" -p "$port" $common $conly "$pip" >"$OUT/${name}.log" 2>&1
    tail -4 "$OUT/${name}.log" | head -2
    sleep 1
}

# bandwidth cases (Gbps, 65536B messages, 10 s duration; -D must match on both sides)
run_case bw_vf0_q1  ib_write_bw mlx5_6 mlx5_6 10.1.0.2 18601 "--report_gbits -s 65536 -q 1 -D 10"  ""
run_case bw_vf0_q64 ib_write_bw mlx5_6 mlx5_6 10.1.0.2 18602 "--report_gbits -s 65536 -q 64 -D 10" ""
run_case bw_vf1_q1  ib_write_bw mlx5_7 mlx5_7 10.1.1.2 18603 "--report_gbits -s 65536 -q 1 -D 10"  ""

# latency case (2B, 10k iterations)
run_case lat_vf0 ib_write_lat mlx5_6 mlx5_6 10.1.0.2 18604 "-s 2 -n 10000" ""

# 4-process concurrent same-pair case
echo "== bw_vf0_p4 =="
for i in 1 2 3 4; do
    ssh -o BatchMode=yes $PEER "setsid nohup $PT/ib_write_bw -F -d mlx5_6 -p 1861$i \
        --report_gbits -s 65536 -q 1 -D 10 >/tmp/s0_bw_vf0_p4_s$i.log 2>&1 </dev/null &"
done
sleep 2
pids=()
for i in 1 2 3 4; do
    timeout 60 $PT/ib_write_bw -F -d mlx5_6 -p 1861$i --report_gbits -s 65536 -q 1 -D 10 10.1.0.2 \
        >"$OUT/bw_vf0_p4_c$i.log" 2>&1 &
    pids+=($!)
done
wait "${pids[@]}"
grep -h "^ 65536" "$OUT"/bw_vf0_p4_c*.log
echo "baseline done: $OUT"
