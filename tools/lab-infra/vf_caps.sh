#!/usr/bin/env bash
# The sold bandwidth of every VF, enforced twice, class-blind, on its own DPU.
#
# Lab shape since 2026-08-26: 8 VFs per host, every VF sold at 50 Gb/s. The
# number comes from the registry policy (max_rate_bps) and is held by two
# layers that know nothing about each other or about HPFT:
#
#   uplink    devlink port function rate tx_max on the VF's representor
#             (hardware scheduler; tools/dpu/hw_maxrate.py --sync)
#   downlink  an OVS drop-band meter on every IP packet addressed to the VF,
#             installed on the VF's own DPU (the naive cloud policer: excess is
#             dropped, nothing is marked, no signal reaches any CC)
#
# Both are installed on EVERY DPU, because any node may send or receive in a
# given experiment. The meter flows match on nw_dst: traffic to a VF arrives
# on some tunnel port, traffic leaving a VF never carries its own IP as
# destination, so no in_port qualifier is needed and the flow survives tunnel
# renames. The meter id is 11 + VF index (11..18), below any id an experiment
# might add by hand.
#
# THREE rules per VF, one per class, all pointing at the same meter, and that
# is not cosmetic. A single nw_dst-only rule at a priority above the receiver
# agent's classification rules (50 RoCE / 45 TCP / 40 ip) resolves the lookup
# without the classifier ever examining nw_proto, so the datapath megaflow it
# produces carries no proto field - and the receiver, which reads its per-class
# arrivals out of exactly those megaflow keys, then books every packet as
# ip_other, schedules nothing, and reports zero for every flow-set (measured
# 2026-08-29: V7 showed 24 flow-sets at 0.00 G with the wire carrying 6 G
# each). Splitting the rule by class forces the lookup to examine the L4
# fields, so the megaflow keeps them. The cap is unchanged: every packet still
# passes exactly one meter action, and it is the same meter.
#
#   vf_caps.sh sync        devlink caps + meters on all DPUs from the policy
#   vf_caps.sh clear       release every devlink cap and delete every meter
#   vf_caps.sh meter-on    meters only (policy rate)
#   vf_caps.sh meter-off   meters only
#   vf_caps.sh devlink-on  devlink only
#   vf_caps.sh devlink-off devlink only
#   vf_caps.sh status      what every DPU is enforcing right now
#
# Prerequisite: the registry on the DPUs matches the repo (deploy_check.sh
# --deploy) - hw_maxrate reads /opt/hpft/lab-registry.json there.
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
REG=config/lab-registry.json
BR=ovsbr-p1

mapfile -t NODES < <(python3 -c "
import json
r=json.load(open('$REG'))
for n in r['nodes']: print(n['host'], n['dpu'])")

# "<idx> <ip> <kbps>" per local VF, from the policy
vf_rows() { python3 -c "
import json,re
r=json.load(open('$REG'))
for v in r['vnics']:
    if v['host']!='$1': continue
    m=r['policy']['vms'].get(v['vnic_id'],{}).get('max_rate_bps')
    if not m: continue
    print(int(re.search(r'vf(\d+)$', v['netdev']).group(1)), v['ip'], int(m)//1000)"; }

meter_on() { # $1 host $2 dpu
  local rows; rows=$(vf_rows "$1")
  ssh -o BatchMode=yes "$2" "while read -r i ip kbps; do
      [ -n \"\$i\" ] || continue
      sudo ovs-ofctl -O OpenFlow13 del-flows $BR \"udp,nw_dst=\$ip,tp_dst=4791\" 2>/dev/null
      sudo ovs-ofctl -O OpenFlow13 del-flows $BR \"tcp,nw_dst=\$ip\" 2>/dev/null
      sudo ovs-ofctl -O OpenFlow13 del-flows $BR \"ip,nw_dst=\$ip\" 2>/dev/null
      sudo ovs-ofctl -O OpenFlow13 del-meter $BR \"meter=\$((11+i))\" 2>/dev/null
      sudo ovs-ofctl -O OpenFlow13 add-meter $BR \"meter=\$((11+i)),kbps,band=type=drop,rate=\$kbps\"
      sudo ovs-ofctl -O OpenFlow13 add-flow $BR \"priority=122,udp,nw_dst=\$ip,tp_dst=4791,actions=meter:\$((11+i)),NORMAL\"
      sudo ovs-ofctl -O OpenFlow13 add-flow $BR \"priority=121,tcp,nw_dst=\$ip,actions=meter:\$((11+i)),NORMAL\"
      sudo ovs-ofctl -O OpenFlow13 add-flow $BR \"priority=120,ip,nw_dst=\$ip,actions=meter:\$((11+i)),NORMAL\"
    done <<'EOF'
$rows
EOF
    echo \"  $1 ($2): \$(sudo ovs-ofctl -O OpenFlow13 dump-meters $BR | grep -c 'meter=1[1-8]') meters\""
}
meter_off() {
  ssh -o BatchMode=yes "$2" "for i in 0 1 2 3 4 5 6 7; do
      sudo ovs-ofctl -O OpenFlow13 dump-flows $BR 2>/dev/null | grep -o 'nw_dst=[0-9.]* actions=meter:'\$((11+i)) | cut -d' ' -f1 | sort -u | while read -r m; do
        sudo ovs-ofctl -O OpenFlow13 del-flows $BR \"udp,\$m,tp_dst=4791\" 2>/dev/null
        sudo ovs-ofctl -O OpenFlow13 del-flows $BR \"tcp,\$m\" 2>/dev/null
        sudo ovs-ofctl -O OpenFlow13 del-flows $BR \"ip,\$m\"; done
      sudo ovs-ofctl -O OpenFlow13 del-meter $BR \"meter=\$((11+i))\" 2>/dev/null
    done; echo '  $1 ($2): meters removed'"
}
devlink_on()  { ssh -o BatchMode=yes "$2" "sudo python3 /opt/hpft/hw_maxrate.py --sync --local-host $1"  | sed "s/^/  $1: /"; }
devlink_off() { ssh -o BatchMode=yes "$2" "sudo python3 /opt/hpft/hw_maxrate.py --clear --local-host $1" | sed "s/^/  $1: /"; }
status() {
  ssh -o BatchMode=yes "$2" "echo '== $1 ($2)'
    sudo python3 /opt/hpft/hw_maxrate.py --show --local-host $1
    sudo ovs-ofctl -O OpenFlow13 dump-meters $BR 2>/dev/null | grep -oE 'meter=1[1-8] kbps .*rate=[0-9]+' | sed 's/^/  /'
    sudo ovs-ofctl -O OpenFlow13 dump-flows $BR 2>/dev/null | grep -oE 'n_packets=[0-9]+, n_bytes=[0-9]+.*nw_dst=[0-9.]+ actions=meter:[0-9]+' | sed 's/^/  /'
    sudo ovs-ofctl -O OpenFlow13 meter-stats $BR 2>/dev/null | grep -E 'meter:1[1-8]|byte_band_count' | paste - - | sed 's/^/  /'"
}

cmd=${1:-status}
for n in "${NODES[@]}"; do
  set -- $n
  case "$cmd" in
    sync)        devlink_on "$1" "$2"; meter_on "$1" "$2" ;;
    clear)       devlink_off "$1" "$2"; meter_off "$1" "$2" ;;
    meter-on)    meter_on "$1" "$2" ;;
    meter-off)   meter_off "$1" "$2" ;;
    devlink-on)  devlink_on "$1" "$2" ;;
    devlink-off) devlink_off "$1" "$2" ;;
    status)      status "$1" "$2" ;;
    *) echo "usage: vf_caps.sh sync|clear|meter-on|meter-off|devlink-on|devlink-off|status"; exit 2 ;;
  esac
done
