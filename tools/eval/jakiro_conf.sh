#!/usr/bin/env bash
# Render + push the Jakiro DHTB conf for an eval scenario (P10).
# Jakiro itself is used UNMODIFIED (eval decision #4); the only thing an
# experiment may legitimately vary is the policy intent: root capacity,
# class weight, and the TCP port its classifier matches.
#
#   jakiro_conf.sh show
#   jakiro_conf.sh set <capacity_gbps> <roce_permille> [tcp_dst_port]
#   jakiro_conf.sh restore          # put back the original (backed up once)
#
# Scenario values (evaluation_plan v3): 2-1-1-R single-dst quota -> 30 500;
# 2-1-1-I incast form -> 92 750 (best-approximation of "3 RDMA tenants vs
# 1 TCP tenant, equal weight"; DHTB has no tenant layer -- that is the
# point). lab_env.sh jakiro (re)starts the DHTB with whatever this wrote.
# DHTB_CFG log line check happens at DHTB start (batch 4), not here.
set -eu
CONF='~/bzx/jakiro_dhtb/jakiro_dhtb.conf'
case "${1:-}" in
  show) ssh hpft-dpu2 "grep -E '^(CAPACITY_GBPS|ROCE_WEIGHT_PERMILLE|TCP_DST_PORT|OVERLAY_DST_IP)=' $CONF" ;;
  set)
    CAP=$2; PERM=$3; TPORT=${4:-5201}
    ssh hpft-dpu2 "[ -f $CONF.orig ] || cp $CONF $CONF.orig
      sed -i -e 's/^CAPACITY_GBPS=.*/CAPACITY_GBPS=$CAP/' \
             -e 's/^ROCE_WEIGHT_PERMILLE=.*/ROCE_WEIGHT_PERMILLE=$PERM/' \
             -e 's/^TCP_DST_PORT=.*/TCP_DST_PORT=$TPORT/' $CONF
      grep -E '^(CAPACITY_GBPS|ROCE_WEIGHT_PERMILLE|TCP_DST_PORT)=' $CONF" ;;
  restore) ssh hpft-dpu2 "[ -f $CONF.orig ] && cp $CONF.orig $CONF && echo restored || echo 'no backup'" ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
