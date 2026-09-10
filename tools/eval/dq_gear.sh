#!/usr/bin/env bash
# Recovery-parameter gears for the software DCQCN that runs underneath
# HyperFront (the cc_rate term of rate = min(cc_rate, level)).
#
#   dq_gear.sh gentle | default | fast          (GEAR_HOST=<host> picks the sender)
#
# The knobs are the same three a firmware DCQCN RP exposes, written through
# the PCC mailbox (0xcce <value> <which>; which 0=AI, 1=HAI, 2=rate timer),
# and the three gears are the firmware gears of tools/eval/dcqcn_gear.sh
# converted to the executor's units, so the HyperFront arm and the Jakiro arm
# of 2-3 run the same three settings on the two implementations:
#
#   gear     rate timer   AI (fw Mb/s -> units)   HAI (fw Mb/s -> units)   fw gear (rpg_time/ai/hai)
#   gentle     1200 us      1 Mb/s ->    5          10 Mb/s ->   52         1200 / 1  / 10
#   default     300 us      5 Mb/s ->   26          50 Mb/s ->  262          300 / 5  / 50
#   fast         75 us     50 Mb/s ->  262         500 Mb/s -> 2621           75 / 50 / 500
#
# Rates are the device's fxp20 units, 2^20 = line rate (200 G): one unit is
# 190.7 kb/s, so 5 Mb/s is 26 units - which is also the executor's built-in
# default (g_dq_ai = 26, g_dq_hai = 262, g_dq_time_us = 300 in
# rp_rtt_template_dev_main.c). The rate timer is evaluated on the 1 ms epoch
# boundary and catches up the elapsed periods in one go, so a 75 us timer takes
# thirteen steps per millisecond rather than one every 75 us; the rate at which
# the stages advance is the firmware's.
set -eu
case "${1:-}" in
  gentle)  AI=5;   HAI=52;   T=1200 ;;
  default) AI=26;  HAI=262;  T=300  ;;
  fast)    AI=262; HAI=2621; T=75   ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac
# Which sender's executor. The gear is device state on ONE DPU, and the RDMA
# sender is not always sgpu01: 2-3 sends RDMA from sgpu03. Name the host, not
# the DPU - the registry knows which DPU belongs to it.
GEAR_HOST=${GEAR_HOST:-$(python3 -c "import json;print(json.load(open('/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-registry.json'))['sender_host'])")}
GEAR_DPU=$(python3 -c "
import json;r=json.load(open('/home/zhaoxiang/hyperfront/hpft-implementation/config/lab-registry.json'))
print({n['host']:n['dpu'] for n in r['nodes']}['$GEAR_HOST'])")
ssh "$GEAR_DPU" "echo '0xcce $AI 0'  > /tmp/rp_fifo; echo '0xcce $HAI 1' > /tmp/rp_fifo; echo '0xcce $T 2' > /tmp/rp_fifo"
echo "dq_gear: $1 on $GEAR_HOST ($GEAR_DPU)  ai=$AI hai=$HAI rate_timer=${T}us"
