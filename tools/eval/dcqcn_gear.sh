#!/usr/bin/env bash
# DCQCN RP recovery-parameter gears for the eval CC-sensitivity experiments
# (evaluation_plan 2-1-2-D). Writes the sender host PF sysfs — the ONLY
# place VF DCQCN parameters follow (bf3-cc-ownership fact); NP side is
# left untouched. Wraps the generic reader/writer ~/hyperfront/dcqcn.sh.
#
#   dcqcn_gear.sh status | gentle | default | fast
#
# gears (rpg_time_reset / rpg_ai_rate / rpg_hai_rate):
#   gentle  1200 / 1  / 10     slow recovery
#   default  300 / 5  / 50     firmware default
#   fast      75 / 50 / 500    aggressive recovery
# Only meaningful for fw-DCQCN arms (UPCC=0); harmless to write under PCC.
set -eu
DC=$HOME/hyperfront/dcqcn.sh
IF=dpu1
case "${1:-}" in
  gentle)  sudo "$DC" $IF rpg_time_reset=1200 rpg_ai_rate=1  rpg_hai_rate=10  | grep rpg_ ;;
  default) sudo "$DC" $IF rpg_time_reset=300  rpg_ai_rate=5  rpg_hai_rate=50  | grep rpg_ ;;
  fast)    sudo "$DC" $IF rpg_time_reset=75   rpg_ai_rate=50 rpg_hai_rate=500 | grep rpg_ ;;
  status)  sudo "$DC" $IF | grep -E "rpg_time_reset|rpg_ai_rate|rpg_hai_rate" ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
