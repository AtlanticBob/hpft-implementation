#!/usr/bin/env bash
# Render + push the Jakiro DHTB conf for an evaluation scenario.
#
# Jakiro is a per-vNIC mechanism: every VM's vNIC on the receiver has its own
# Jakiro - its own root bucket, class buckets and decision - so the conf names
# one Jakiro per VF of the receiving host (address, representor, MAC, label),
# all with the same policy unless a scenario says otherwise. The authors'
# decision logic is untouched; the two adaptations this testbed needed are in
# jakiro_multipeer.md. What an experiment may vary is the policy intent and
# which traffic the classifier is pointed at: root capacity, class weight, the
# peers whose tunnelled traffic reaches the DHTB, and the TCP port.
#
#   jakiro_conf.sh show
#   jakiro_conf.sh set <capacity_gbps> <roce_permille> [peers] [tcp_dst_port]
#   jakiro_conf.sh restore          # put back the original (backed up once)
#
# The per-vNIC lists (OVERLAY_DST_IP, JAKIRO_REP_PCI, JAKIRO_DST_MAC,
# JAKIRO_LABELS) are read from the receiving host at `set` time: a VF's MAC and
# PCI address are whatever vf_setup gave it, not what the registry remembers.
# peers defaults to every OTHER node's underlay address, comma separated: the
# gate is an exact match per peer and traffic from a peer with no entry
# bypasses the DHTB entirely.
#
# Scenario values (evaluation_plan.md): 2-2b and 2-3 -> 50 500 (a 50 G VM
# quota, the two classes equally weighted); 2-8b also runs 50 750.
# lab_env.sh jakiro (re)starts the DHTB with whatever this wrote; the DHTB_CFG
# lines in its log (one per vNIC) are the readback.
set -eu
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
reg() { python3 -c "import json;r=json.load(open('$REPO/config/lab-registry.json'));print($1)"; }
RHOST=${RHOST:-$(reg "r['receiver_host']")}
RDPU=${RDPU:-$(reg "{n['host']:n['dpu'] for n in r['nodes']}['$RHOST']")}
PEERS_DEFAULT=$(reg "','.join(n['underlay_ip'] for n in r['nodes'] if n['host']!=r['receiver_host'])")
CONF='~/bzx/jakiro_dhtb/jakiro_dhtb.conf'
KEYS='CAPACITY_GBPS|ROCE_WEIGHT_PERMILLE|TCP_DST_PORT|OVERLAY_DST_IP|UNDERLAY_SRC_IP|UNDERLAY_DST_IP|JAKIRO_REP_PCI|JAKIRO_DST_MAC|JAKIRO_LABELS|REP'
case "${1:-}" in
  show) ssh "$RDPU" "grep -E '^($KEYS)=' $CONF" ;;
  set)
    CAP=$2; PERM=$3; PEERS=${4:-$PEERS_DEFAULT}; TPORT=${5:-5201}
    # one Jakiro per VF of the receiving host, in VF order: label, address
    # (registry), representor PCI and MAC (read live on the host)
    VNICS=$(reg "' '.join('%s,%s' % (v['netdev'], v['ip']) for v in sorted((v for v in r['vnics'] if v['host']=='$RHOST'), key=lambda v: int(v['netdev'].rsplit('vf',1)[1])))")
    LISTS=$(ssh -n -o BatchMode=yes "$RHOST" "for p in $VNICS; do d=\${p%%,*}; ip=\${p#*,}
        echo \"\${d#dpu1} \$ip \$(basename \$(readlink /sys/class/net/\$d/device)) \$(cat /sys/class/net/\$d/address)\"; done")
    [ -n "$LISTS" ] || { echo "jakiro_conf: could not read the VFs of $RHOST"; exit 1; }
    LABELS=$(echo "$LISTS" | awk '{printf "%s%s", s, $1; s=","}')
    IPS=$(echo "$LISTS" | awk '{printf "%s%s", s, $2; s=","}')
    PCIS=$(echo "$LISTS" | awk '{printf "%s%s", s, $3; s=","}')
    MACS=$(echo "$LISTS" | awk '{printf "%s%s", s, $4; s=","}')
    ssh "$RDPU" "[ -f $CONF.orig ] || cp $CONF $CONF.orig
      grep -vE '^(CAPACITY_GBPS|ROCE_WEIGHT_PERMILLE|TCP_DST_PORT|OVERLAY_DST_IP|UNDERLAY_SRC_IP|JAKIRO_REP_PCI|JAKIRO_DST_MAC|JAKIRO_LABELS)=' $CONF > $CONF.new
      cat >> $CONF.new <<CONF
# one Jakiro per vNIC (jakiro_conf.sh set, $(date +%F))
JAKIRO_LABELS=$LABELS
OVERLAY_DST_IP=$IPS
JAKIRO_REP_PCI=$PCIS
JAKIRO_DST_MAC=$MACS
CAPACITY_GBPS=$CAP
ROCE_WEIGHT_PERMILLE=$PERM
UNDERLAY_SRC_IP=$PEERS
TCP_DST_PORT=$TPORT
CONF
      mv $CONF.new $CONF
      grep -E '^($KEYS)=' $CONF" ;;
  restore) ssh "$RDPU" "[ -f $CONF.orig ] && cp $CONF.orig $CONF && echo restored || echo 'no backup'" ;;
  *) sed -n '2,29p' "$0"; exit 2 ;;
esac
