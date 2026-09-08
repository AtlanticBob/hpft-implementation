#!/usr/bin/env bash
# Measure the RDMA executor's flowtag of straight pairs src/vfK -> dst/vfK.
#
# The PCC RP identifies a pair by a hardware flowtag. It is independent of
# the SOURCE card but not of the destination (2026-09-04: vf0>vf0 reads
# 0x74249a41 towards sgpu02 and 0x1e3dac89 towards sgpu04), so every host
# that receives needs its own rows in the registry's rdma_flowtags. This
# script runs a short two-QP flow per pair while the RP has no entry for it
# and reads the RP's "last unknown flowtag" (mailbox 0xdea). A pair that is
# already registered does not update that register, so probe before adding
# the rows, or clear them first.
#
#   flowtag_probe.sh <src_host> <dst_host> [vf indices, default 0-7]
# prints "src/vfK>dst/vfK 0x........" per pair; feed the lines to
# registry_flowtags.py to insert them.
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
SRC=${1:?src host}; DST=${2:?dst host}; shift 2
VFS=${*:-0 1 2 3 4 5 6 7}
PT=$HOME/hyperfront/perftest-enhanced/ib_write_bw
reg() { python3 -c "import json;r=json.load(open('config/lab-registry.json'));print($1)"; }
SDPU=$(reg "{n['host']:n['dpu'] for n in r['nodes']}['$SRC']")
port=27600
for k in $VFS; do
  sdev=$(reg "next(v['rdma_dev'] for v in r['vnics'] if v['vnic_id']=='$SRC/vf$k')")
  ddev=$(reg "next(v['rdma_dev'] for v in r['vnics'] if v['vnic_id']=='$DST/vf$k')")
  dip=$(reg "next(v['ip'] for v in r['vnics'] if v['vnic_id']=='$DST/vf$k')")
  port=$((port+1))
  if [ "$DST" = "$(hostname)" ]; then (setsid nohup $PT -d $ddev -q 2 -m 1024 -p $port --report_gbits -D 4 >/tmp/ftprobe_s.log 2>&1 </dev/null &)
  else ssh -n -o BatchMode=yes "$DST" "setsid nohup $PT -d $ddev -q 2 -m 1024 -p $port --report_gbits -D 4 >/tmp/ftprobe_s.log 2>&1 </dev/null &" </dev/null; fi
  sleep 1.5
  if [ "$SRC" = "$(hostname)" ]; then ($PT -d $sdev -q 2 -m 1024 -p $port --report_gbits -D 4 $dip >/tmp/ftprobe_c.log 2>&1 &)
  else (ssh -n -o BatchMode=yes "$SRC" "$PT -d $sdev -q 2 -m 1024 -p $port --report_gbits -D 4 $dip >/tmp/ftprobe_c.log 2>&1" </dev/null &); fi
  sleep 2
  tag=$(ssh -o BatchMode=yes "$SDPU" 'pos=$(sudo stat -c %s /tmp/pcc_rp.log)
    sudo python3 -c "import os; fd=os.open(\"/tmp/rp_fifo\", os.O_WRONLY); os.write(fd, b\"0xdea 0\n\"); os.close(fd)"
    sleep 0.4; sudo tail -c +$((pos+1)) /tmp/pcc_rp.log | grep -o "HPFT_RSP ft=0x[0-9a-f]*" | tail -1 | cut -d= -f2')
  echo "$SRC/vf$k>$DST/vf$k ${tag:-PROBE_FAILED}"
  sleep 3
done
