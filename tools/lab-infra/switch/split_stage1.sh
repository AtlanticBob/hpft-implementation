#!/usr/bin/env bash
# Stage 1 of splitting sn5600 in two (README.md in this directory): safe to run
# BEFORE the loopback cable is plugged. Defines VLANs 101/102, puts swp21 in
# 101 and swp25 in 102, and filters BPDUs on both so RSTP does not block the
# loop. Host ports are NOT touched here - connectivity between the four hosts
# is unchanged until split_cutover.sh.
set -eu
ssh -o BatchMode=yes sn5600 '
  nv set bridge domain br_default vlan 101,102
  nv set interface swp21,swp25 type swp
  nv set interface swp21,swp25 link mtu 9216
  nv set interface swp21,swp25 link state up
  nv set interface swp21 bridge domain br_default access 101
  nv set interface swp25 bridge domain br_default access 102
  nv set interface swp21,swp25 bridge domain br_default stp bpdu-filter on
  nv set interface swp21,swp25 bridge domain br_default stp admin-edge on
  nv config apply -y
  echo "== vlans"; nv show bridge domain br_default vlan | grep -E "^10[012]"
  echo "== loop ports"; nv show interface --view=brief | grep -E "^swp2[15]\b"
  echo "== stp on loop ports"
  nv show interface swp21 bridge domain br_default stp 2>/dev/null | grep -iE "bpdu-filter|admin-edge|state"
  nv show interface swp25 bridge domain br_default stp 2>/dev/null | grep -iE "bpdu-filter|admin-edge|state"' 2>&1 | grep -v Welcome
