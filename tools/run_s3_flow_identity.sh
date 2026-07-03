#!/usr/bin/env bash
# S3: flow-identity probe. Requires the S3-patched PCC binary on the DPU.
# Starts PCC with per-flow first-event tracing, then 5 staggered known flows;
# saves client perftest outputs (local QPNs) and the PCC trace log.
set -u
PT=/home/zhaoxiang/hyperfront/perftest-26015/ib_write_bw
OUT=${1:?usage: run_s3_flow_identity.sh <output_dir>}
mkdir -p "$OUT"

echo "== starting PCC (100 s window) =="
ssh -o BatchMode=yes hpft-dpu 'setsid sudo nohup timeout -s INT 110 \
    /home/ubuntu/bzx/doca34-apps/build/pcc/doca_pcc -d mlx5_0 -l 50 -w 100 \
    >/tmp/pcc_s3.log 2>&1 </dev/null & sleep 5; grep -c "user init" /tmp/pcc_s3.log'

flow() {
    # $1 tag, $2 port, $3 local dev, $4 remote dev, $5 peer ip, $6 qps
    ssh -o BatchMode=yes sgpu02 "setsid nohup $PT -F -d $4 -p $2 -s 65536 -q $6 --report_gbits -D 25 \
        >/tmp/s3_${1}_server.log 2>&1 </dev/null & sleep 1"
    (timeout 50 $PT -F -d "$3" -p "$2" -s 65536 -q "$6" --report_gbits -D 25 "$5" \
        >"$OUT/${1}_client.log" 2>&1 && echo "$1 done") &
}

flow F1_vf0      18541 mlx5_6 mlx5_6 10.1.0.2  1; sleep 3
flow F2_vf1      18542 mlx5_7 mlx5_7 10.1.1.2  1; sleep 3
flow F3_pf_dpu0  18543 mlx5_2 mlx5_2 101.2.0.2 1; sleep 3
flow F4_pf_dpu1  18544 mlx5_3 mlx5_3 101.2.1.2 1; sleep 3
flow F5_vf0_2qp  18545 mlx5_6 mlx5_6 10.1.0.2  2
wait
echo "== flows finished; collecting =="
for f in "$OUT"/F*_client.log; do
    echo "-- $(basename $f)"
    grep -E "local address|^ 65536" "$f" | head -4
done
ssh -o BatchMode=yes hpft-dpu 'sleep 20; sudo pkill -INT -f "doca_pcc -d" 2>/dev/null; sleep 2; cat /tmp/pcc_s3.log' \
    > "$OUT/pcc_s3_trace.log" 2>&1
echo "== format-1 trace lines:"
grep "format 1" "$OUT/pcc_s3_trace.log" | head -30
