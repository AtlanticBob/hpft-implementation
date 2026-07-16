#!/usr/bin/env bash
# Environment/knob switching for motivation-1.2. Source or run with args.
#   ecn neutral|aggr|gentle   -- bind ECN profile on swp37s0 (creates on demand)
#   tcp default|slow|fast     -- cubic beta on sgpu01 (717 | 410 | 922)
#   rdma default|slow|fast    -- DCQCN RP recovery knobs (location: see RDMA_FN)
set -e

ecn() {
  case "$1" in
  off)
    ssh sn5600 'nv set interface swp37s0 qos congestion-control profile ecn_debug; nv config apply -y' ;;
  neutral)
    ssh sn5600 'nv set interface swp37s0 qos congestion-control profile ecn_incast_bzx; nv config apply -y' ;;
  aggr)
    ssh sn5600 'nv set qos congestion-control motiv12_aggr traffic-class 0 ecn enable
nv set qos congestion-control motiv12_aggr traffic-class 0 red disable
nv set qos congestion-control motiv12_aggr traffic-class 0 min-threshold 25000
nv set qos congestion-control motiv12_aggr traffic-class 0 max-threshold 100000
nv set qos congestion-control motiv12_aggr traffic-class 0 probability 80
nv set qos congestion-control motiv12_aggr traffic-class 3 ecn enable
nv set qos congestion-control motiv12_aggr traffic-class 3 red disable
nv set qos congestion-control motiv12_aggr traffic-class 3 min-threshold 25000
nv set qos congestion-control motiv12_aggr traffic-class 3 max-threshold 100000
nv set qos congestion-control motiv12_aggr traffic-class 3 probability 80
nv set interface swp37s0 qos congestion-control profile motiv12_aggr
nv config apply -y' ;;
  gentle)
    ssh sn5600 'nv set qos congestion-control motiv12_gentle traffic-class 0 ecn enable
nv set qos congestion-control motiv12_gentle traffic-class 0 red disable
nv set qos congestion-control motiv12_gentle traffic-class 0 min-threshold 400000
nv set qos congestion-control motiv12_gentle traffic-class 0 max-threshold 1600000
nv set qos congestion-control motiv12_gentle traffic-class 0 probability 5
nv set qos congestion-control motiv12_gentle traffic-class 3 ecn enable
nv set qos congestion-control motiv12_gentle traffic-class 3 red disable
nv set qos congestion-control motiv12_gentle traffic-class 3 min-threshold 400000
nv set qos congestion-control motiv12_gentle traffic-class 3 max-threshold 1600000
nv set qos congestion-control motiv12_gentle traffic-class 3 probability 5
nv set interface swp37s0 qos congestion-control profile motiv12_gentle
nv config apply -y' ;;
  esac
  ssh sn5600 'nv show interface swp37s0 qos congestion-control' 2>/dev/null | grep -v Welcome | tail -8
}

tcp() {
  case "$1" in
  default) B=717 ;;
  slow)    B=410 ;;
  fast)    B=922 ;;
  esac
  echo $B | sudo tee /sys/module/tcp_cubic/parameters/beta
}

# RDMA RP knobs live on the HOST PF (sgpu01 netdev dpu1 = 0000:38:00.1):
# probing showed VF QPs obey this function's RP params under fw DCQCN
# (Arm-side p0/p1 sysfs and debugfs have no effect on VF traffic).
RDMA_FN=/sys/class/net/dpu1/ecn/roce_rp
rdma() {
  case "$1" in
  default) T=300;  A=5;  H=50  ;;
  slow)    T=1200; A=1;  H=10  ;;
  fast)    T=75;   A=50; H=500 ;;
  esac
  echo $T | sudo tee $RDMA_FN/rpg_time_reset
  echo $A | sudo tee $RDMA_FN/rpg_ai_rate
  echo $H | sudo tee $RDMA_FN/rpg_hai_rate
}

"$@"
