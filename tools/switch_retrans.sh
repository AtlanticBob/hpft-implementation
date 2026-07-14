#!/usr/bin/env bash
# One-shot RDMA retransmit-mode switch: gbn|sr.
# Sets RDMA_SELECTIVE_REPEAT_EN on both BF3 DPUs, reboots both, waits for
# them to return, then runs reboot_recover.sh to restore the lab. Run on the
# sender host (sgpu01). ~4-6 min total.
#   sr  -> RDMA_SELECTIVE_REPEAT_EN=1 (selective repeat)
#   gbn -> RDMA_SELECTIVE_REPEAT_EN=0 (go-back-N, default)
set -u
MODE="${1:-}"
case "$MODE" in
  sr)  VAL=1 ;;
  gbn) VAL=0 ;;
  *) echo "usage: $0 gbn|sr"; exit 1 ;;
esac
REPO=/home/zhaoxiang/hyperfront/hpft-v2
MST=/dev/mst/mt41692_pciconf0

echo "== set RDMA_SELECTIVE_REPEAT_EN=$VAL ($MODE) on both DPUs =="
for h in hpft-dpu hpft-dpu2; do
  ssh "$h" "sudo mlxconfig -y -d $MST set RDMA_SELECTIVE_REPEAT_EN=$VAL" >/dev/null 2>&1 \
    && echo "  $h set" || echo "  $h SET FAILED"
done

echo "== reboot both DPUs =="
ssh hpft-dpu 'sudo reboot' 2>/dev/null &
ssh hpft-dpu2 'sudo reboot' 2>/dev/null &
sleep 30

echo "== wait for both DPUs to return =="
back=0
for i in $(seq 1 30); do
  sleep 15
  u1=$(timeout 8 ssh -o ConnectTimeout=5 -o BatchMode=yes hpft-dpu  'echo up' 2>/dev/null)
  u2=$(timeout 8 ssh -o ConnectTimeout=5 -o BatchMode=yes hpft-dpu2 'echo up' 2>/dev/null)
  echo "  t=$((i*15+30))s dpu=$u1 dpu2=$u2"
  [ "$u1" = up ] && [ "$u2" = up ] && { back=1; break; }
done
[ "$back" = 1 ] || { echo "DPUs did not return -- check console"; exit 1; }

echo "== recover lab environment =="
sleep 10
bash "$REPO/tools/reboot_recover.sh"
