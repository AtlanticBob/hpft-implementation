#!/usr/bin/env bash
# Recovery-parameter gears for the software DCQCN that runs underneath
# HyperFront (the cc_rate term of rate = min(cc_rate, level)).
#
#   dq_gear.sh gentle | default | fast
#
# The knobs are the same three a firmware DCQCN RP exposes, written through
# the PCC mailbox (0xcce <value> <which>; which 0=AI, 1=HAI, 2=rate timer):
#
#   gear     rate timer   AI            HAI           mirrors fw gear
#   gentle     3000 us    MAX/2000      MAX/400       rpg 1200/1/10
#   default     300 us    MAX/400       MAX/80        rpg  300/5/50
#   fast        300 us    MAX/80        MAX/16        rpg   75/50/500
#
# Rates are the device's fxp20 units, MAX = 1<<20 = line rate. The rate
# timer is checked on the 1 ms epoch boundary, so its effective period is
# max(1 ms, value) -- gentle and default therefore differ by their step
# sizes plus a ~2.7 ms vs ~1 ms recovery cadence, not by the raw ratio.
set -eu
MAX=1048576
case "${1:-}" in
  gentle)  AI=$((MAX/2000)); HAI=$((MAX/400)); T=3000 ;;
  default) AI=$((MAX/400));  HAI=$((MAX/80));  T=300  ;;
  fast)    AI=$((MAX/80));   HAI=$((MAX/16));  T=300  ;;
  *) sed -n '2,18p' "$0"; exit 2 ;;
esac
ssh hpft-dpu "echo '0xcce $AI 0'  > /tmp/rp_fifo; echo '0xcce $HAI 1' > /tmp/rp_fifo; echo '0xcce $T 2' > /tmp/rp_fifo"
echo "dq_gear: $1  ai=$AI hai=$HAI rate_timer=${T}us"
