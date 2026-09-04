#!/usr/bin/env bash
# Undo the split (README.md): host ports back into VLAN 100, loop ports down.
# VLAN 101/102 definitions and the loop ports' bpdu-filter are left in place -
# they are harmless with the ports down and save re-typing next time.
set -eu
ssh -o BatchMode=yes sn5600 '
  nv set interface swp37s0-1,swp3s1,swp4s1 bridge domain br_default access 100
  nv set interface swp21,swp25 link state down
  nv config apply -y
  for p in swp37s1 swp3s1 swp37s0 swp4s1; do printf "%s access vlan: " $p; nv show interface $p bridge domain br_default access 2>/dev/null | tail -1; done' 2>&1 | grep -v Welcome
