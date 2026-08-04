#!/usr/bin/env bash
# Restore the shared sn5600 to the lab standing state after any eval
# experiment that touched switch QoS (P11). Standing state:
#   swp37s0: ECN profile ecn_incast_bzx, no egress-scheduler, no pfc profile
#   swp37s1: nothing bound
# Profile DEFINITIONS (motiv_default_ecn, eval_split5050/7525, ...) are
# kept -- binding is per-experiment, defined in each EXECUTION.md.
set -eu
ssh sn5600 '
  nv set interface swp37s0 qos congestion-control profile ecn_incast_bzx
  nv unset interface swp37s0 qos egress-scheduler 2>/dev/null || true
  nv unset interface swp37s1 qos egress-scheduler 2>/dev/null || true
  nv unset interface swp37s0 qos pfc 2>/dev/null || true
  nv unset interface swp37s1 qos pfc 2>/dev/null || true
  nv unset interface swp37s0 qos mapping 2>/dev/null || true
  nv unset interface swp37s1 qos mapping 2>/dev/null || true
  nv config apply -y >/dev/null 2>&1
  echo "== swp37s0 =="
  nv show interface swp37s0 qos congestion-control | grep profile
  nv show interface swp37s0 qos egress-scheduler 2>/dev/null | grep -i profile || echo "scheduler: none"
  echo "== swp37s1 =="
  nv show interface swp37s1 qos egress-scheduler 2>/dev/null | grep -i profile || echo "scheduler: none"' 2>&1 | grep -v Welcome
