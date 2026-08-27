#!/usr/bin/env bash
# After lab_env.sh ztr: replace the stock ZTR binary on the three SENDER DPUs
# with the HPFT executor (no agents), pick the CC term, switch HyperFront off
# (auto-register every flow at MAX, min() combiner, unknown cap = MAX).
#   setup_senders.sh <algo 0|1|2|3> [dpus...]
ALGO=${1:-3}; shift
DPUS=${*:-"hpft-dpu hpft-dpu3 hpft-dpu4"}
HERE=$(cd "$(dirname "$0")" && pwd)
for d in $DPUS; do
  scp -q "$HERE/swift_poll.sh" "$d:/tmp/swift_poll.sh"
  ssh -n -o BatchMode=yes "$d" 'bash /opt/hpft/rp_service.sh start 2>&1 | tail -1' </dev/null
  for w in "0xccd $ALGO" "0xcca 0" "0xccf 1048576" "0xcd0 1"; do
    ssh -n -o BatchMode=yes "$d" "echo '$w' > /tmp/rp_fifo" </dev/null; sleep 0.2
  done
  echo "$d: algo=$ALGO $(ssh -n $d 'pgrep -ax doca_pcc | head -1; grep -ac HPFT_SET /tmp/pcc_rp.log' </dev/null | tr '\n' ' ')"
done
