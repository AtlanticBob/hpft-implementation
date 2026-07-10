#!/usr/bin/env bash
# Soak traffic bundle (cron */15): rotates rdma/tcp/dual against the live
# E stack; logs client-reported rates. Observation only - no restarts.
LOG=/home/zhaoxiang/hyperfront/hpft-shaper-v2/results/soak_20260710/traffic.log
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
TS=$(date -u +%FT%TZ)
case $(( $(date +%s) / 900 % 3 )) in
  0) MODE=rdma ;; 1) MODE=tcp ;; 2) MODE=dual ;;
esac
{
  echo "=== $TS mode=$MODE"
  if [ "$MODE" != tcp ]; then
    # pkill and server start MUST be separate ssh commands: in one shell
    # the pkill regex matches the server invocation text on its own
    # command line and kills the shell (root cause of 9/9 failed rounds)
    ssh -o BatchMode=yes -o ConnectTimeout=10 sgpu02 "pkill -f 'ib_write_b[w].*18519' 2>/dev/null; true"
    ssh -o BatchMode=yes -o ConnectTimeout=10 -f sgpu02 "$PT -d mlx5_6 -p 18519 --report_gbits -D 60 > /tmp/soak_server.log 2>&1"
    sleep 3
  fi
  if [ "$MODE" != rdma ]; then
    iperf3 -c 10.1.0.2 -t 60 -J > /tmp/soak_iperf.json 2>&1 &
  fi
  if [ "$MODE" != tcp ]; then
    $PT -d mlx5_6 -p 18519 --report_gbits -D 60 10.1.0.2 > /tmp/soak_rdma.log 2>&1 &
  fi
  wait
  [ "$MODE" != tcp ] && awk '/65536/ && !/iteration/ {print "rdma_gbps="$4}' /tmp/soak_rdma.log
  [ "$MODE" != rdma ] && python3 -c "import json;print('tcp_gbps=%.2f'%(json.load(open('/tmp/soak_iperf.json'))['end']['sum_received']['bits_per_second']/1e9))" 2>/dev/null
} >> $LOG 2>&1
