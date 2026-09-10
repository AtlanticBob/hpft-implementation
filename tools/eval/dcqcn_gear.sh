#!/usr/bin/env bash
# DCQCN RP recovery-parameter gears for the firmware-DCQCN arms (2-3's Jakiro
# arm, and any plain arm that wants a gear). Writes the sender host's PF sysfs
# — the ONLY place VF DCQCN parameters follow (bf3-cc-ownership fact); the NP
# side is left untouched.
#
#   dcqcn_gear.sh status | gentle | default | fast   (GEAR_HOST=<host> picks the sender)
#
# gears (rpg_time_reset / rpg_ai_rate / rpg_hai_rate):
#   gentle  1200 / 1  / 10     slow recovery
#   default  300 / 5  / 50     firmware default
#   fast      75 / 50 / 500    aggressive recovery
#
# The executor's three gears (tools/eval/dq_gear.sh) are these three converted
# to its own units, so 2-3's two arms run the same settings on the two
# implementations. Only meaningful when the firmware owns CC (UPCC=0);
# harmless to write under PCC.
#
# Two things this script must not assume (both cost a run on 2026-09-10):
#  - the PF netdev is NOT called the same thing everywhere. It carries the VFs,
#    so it is resolved from dpu1vf0's physfn: `dpu1` on sgpu01/sgpu02, `bf1_1`
#    on sgpu03/sgpu04.
#  - the target host does NOT have a current checkout. The reader/writer is
#    shipped from this repo to the target's /tmp for the call instead of
#    running whatever copy happens to be there (the copies on sgpu03/sgpu04
#    were six weeks stale, and the generic dcqcn.sh was missing entirely).
set -eu
REPO=$(cd "$(dirname "$0")/.." && pwd)
DC=$REPO/lab-infra/dcqcn.sh
case "${1:-}" in
  gentle)  ARGS="rpg_time_reset=1200 rpg_ai_rate=1  rpg_hai_rate=10"  ;;
  default) ARGS="rpg_time_reset=300  rpg_ai_rate=5  rpg_hai_rate=50"  ;;
  fast)    ARGS="rpg_time_reset=75   rpg_ai_rate=50 rpg_hai_rate=500" ;;
  status)  ARGS="" ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
HOST=${GEAR_HOST:-$(hostname)}
REMOTE=/tmp/hpft_dcqcn.sh
if [ "$HOST" = "$(hostname)" ]; then
  cp "$DC" "$REMOTE"
else
  scp -q "$DC" "$HOST:$REMOTE"
fi
run() { if [ "$HOST" = "$(hostname)" ]; then bash -c "$1"; else ssh -n -o BatchMode=yes "$HOST" "$1" </dev/null; fi; }
IF=$(run 'p=$(readlink -f /sys/class/net/dpu1vf0/device/physfn); ls $p/net' | tr -d '\r')
[ -n "$IF" ] || { echo "dcqcn_gear: cannot find the PF netdev that owns dpu1vf0 on $HOST" >&2; exit 1; }
run "chmod +x $REMOTE; sudo $REMOTE $IF $ARGS" | grep -E "rpg_time_reset|rpg_ai_rate|rpg_hai_rate"
echo "dcqcn_gear: ${1} on $HOST ($IF)"
