#!/usr/bin/env bash
# Populate the EDT program's helper maps (ip_to_vnic, pair_state, and
# ifindex_to_vnic where resolvable) from the tcp registry plan.
# Why this exists: tcp-shaper-apply runs its tc commands BEFORE its map
# updates and aborts on the (host-absent) p1 egress step, so after any
# host reboot the program is loaded and attached but every packet exits
# at `if (!cfg || !state ...) return TC_ACT_OK` -- TCP runs unpaced while
# the law, the shim and pair_cfg all look healthy.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
B=${HPFT_BPFTOOL:-/usr/lib/linux-tools-5.15.0-185/bpftool}
PIN=/sys/fs/bpf/hpft_tcp_edt/maps
sudo HPFT_BPFTOOL=$B python3 $REPO/tools/tcp_shaper/tools/tcp-shaper-apply \
  --registry $REPO/config/lab-tcp-registry.json --local-host "$(hostname)" \
  --bpftool $B --egress-dev dpu1vf0 --json 2>/dev/null | python3 -c "
import json,sys,subprocess
p=json.load(sys.stdin); B='$B'; n=0
for u in p.get('map_updates',[]):
    m=u['map']
    if 'pair_cfg' in m: continue            # the shim owns pair_cfg
    if u['summary'].get('status')=='not_resolved': continue
    kh=u['key_hex'] if isinstance(u['key_hex'],list) else u['key_hex'].split()
    vh=u['value_hex'] if isinstance(u['value_hex'],list) else u['value_hex'].split()
    r=subprocess.run(['sudo',B,'map','update','pinned',f'$PIN/{m}','key','hex']+kh+['value','hex']+vh,capture_output=True,text=True)
    n+= (r.returncode==0)
print('edt_maps_ensure: %d entries written' % n)
"
for m in hpft_ip_to_vnic hpft_pair_state; do printf "  %s=%s " $m "$(sudo $B map dump pinned $PIN/$m 2>/dev/null | grep -c key)"; done; echo
