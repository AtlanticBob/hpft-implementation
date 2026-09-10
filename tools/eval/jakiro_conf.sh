#!/usr/bin/env bash
# Render + push the Jakiro DHTB conf for an evaluation scenario.
#
# Jakiro's mechanism is used as the authors wrote it (the one adaptation this
# testbed needed is documented in jakiro_multipeer.md). What an experiment may
# legitimately vary is the policy intent and which traffic the classifier is
# pointed at: root capacity, class weight, the Jakiro VF's overlay address, the
# peers whose tunnelled traffic reaches it, and the TCP port its classifier
# matches.
#
#   jakiro_conf.sh show
#   jakiro_conf.sh set <capacity_gbps> <roce_permille> [overlay_dst_ip] [peers] [tcp_dst_port]
#   jakiro_conf.sh restore          # put back the original (backed up once)
#
# peers defaults to every OTHER node's underlay address, comma separated, which
# is what a scenario with several senders needs: the gate is an exact match per
# peer and traffic from a peer with no entry bypasses the DHTB entirely.
#
# Scenario values (evaluation_plan.md): 2-2b and 2-3 -> 50 500 (a 50 G VF quota,
# the two classes equally weighted); 2-8b also runs 50 750.
# lab_env.sh jakiro (re)starts the DHTB with whatever this wrote; the DHTB_CFG
# line in its log is the readback.
set -eu
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
reg() { python3 -c "import json;r=json.load(open('$REPO/config/lab-registry.json'));print($1)"; }
RDPU=${RDPU:-$(reg "{n['host']:n['dpu'] for n in r['nodes']}[r['receiver_host']]")}
PEERS_DEFAULT=$(reg "','.join(n['underlay_ip'] for n in r['nodes'] if n['host']!=r['receiver_host'])")
CONF='~/bzx/jakiro_dhtb/jakiro_dhtb.conf'
case "${1:-}" in
  show) ssh "$RDPU" "grep -E '^(CAPACITY_GBPS|ROCE_WEIGHT_PERMILLE|TCP_DST_PORT|OVERLAY_DST_IP|UNDERLAY_SRC_IP|UNDERLAY_DST_IP|REP)=' $CONF" ;;
  set)
    CAP=$2; PERM=$3; ODST=${4:-10.1.0.2}; PEERS=${5:-$PEERS_DEFAULT}; TPORT=${6:-5201}
    ssh "$RDPU" "[ -f $CONF.orig ] || cp $CONF $CONF.orig
      sed -i -e 's/^CAPACITY_GBPS=.*/CAPACITY_GBPS=$CAP/' \
             -e 's/^ROCE_WEIGHT_PERMILLE=.*/ROCE_WEIGHT_PERMILLE=$PERM/' \
             -e 's|^OVERLAY_DST_IP=.*|OVERLAY_DST_IP=$ODST|' \
             -e 's/^UNDERLAY_SRC_IP=.*/UNDERLAY_SRC_IP=$PEERS/' \
             -e 's/^TCP_DST_PORT=.*/TCP_DST_PORT=$TPORT/' $CONF
      grep -E '^(CAPACITY_GBPS|ROCE_WEIGHT_PERMILLE|TCP_DST_PORT|OVERLAY_DST_IP|UNDERLAY_SRC_IP)=' $CONF" ;;
  restore) ssh "$RDPU" "[ -f $CONF.orig ] && cp $CONF.orig $CONF && echo restored || echo 'no backup'" ;;
  *) sed -n '2,24p' "$0"; exit 2 ;;
esac
