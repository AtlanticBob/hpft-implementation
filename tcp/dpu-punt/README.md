# T3.2 — DPU-transparent TCP shaping via custom DOCA Flow app (feasibility study)

Status (2026-07-06): **paused / consolidated**. This is the reference implementation
from the T3.2 exploration. Full analysis in [`../../docs/t32_dpu_tcp_design.md`](../../docs/t32_dpu_tcp_design.md).

## What it is
A standalone DOCA Flow switch-mode app that selectively punts vf0 TCP to the Arm
CPU and re-injects it to the wire — a bump-in-the-wire base for sender-side TCP
pacing on the DPU, coexisting with OVS (other VFs / RoCE) and the RDMA PCC.

## What was proven to work
- Selective **IPv4+TCP punt to Arm CPU** (RSS queue 0), `fdb_def_rule_en=1` so
  unmatched traffic (RoCE, non-TCP, other VFs) misses → kernel FDB (OVS). Coexists
  with OVS + RDMA/PCC, no regression (ICMP on all 4 VFs verified).
- **CPU TX re-inject** datapath (`doca_eth_txq`): rx=tx, no loop, no crash.
- **Bidirectional TCP completes transparently at line rate** when return traffic
  is not mis-punted.

## The wall (definitive)
Under `fdb_def_rule_en=1` (required for OVS coexistence) **only `parser_meta` type
registers match at any pipe level (root or child); deep header fields — `outer.ip4`
src/dst, TCP ports, `parser_meta.port_id` source vport — are unavailable** (outer.ip4
silently matches 0; port_id crashes). So **direction / src-vnic / 5-tuple selection is
impossible in coexistence mode**; it needs `fdb_def_rule_en=0` (full FDB takeover =
re-implement transparent L2 forwarding for all VFs). Hence this punt is all-TCP
(both directions); direction-selective punt + EDT pacing await that takeover.
Reference samples that *do* deep-match at root (`flow_switch_rss`, `flow_switch_to_wire`)
work only because they use `fdb_def_rule_en=0`.

## Build & run (on the DPU, hpft-dpu)
Source is copied into the DOCA samples tree; build tree is `/tmp/tpb`:
```
sudo cp flow_tcp_punt_{sample,main}.c /opt/mellanox/doca/samples/doca_flow/flow_tcp_punt/
cd /tmp/tpb && sudo ninja
sudo ./doca_flow_tcp_punt -a pci/03:00.1      # runs 30s, prints punt/reinject counters
```
Dev args are baked in main.c: `dv_flow_en=2,fdb_def_rule_en=1,dv_xmeta_en=4`.
Needs hugepages. The app is not a service (runs 30s and exits).

## To revive (path C)
Switch to `fdb_def_rule_en=0`, own the FDB, add transparent dst-MAC L2 forwarding
for all VFs/hpf/uplink, then reintroduce the src-vnic/direction match + EDT pacing
(replicate opt3: shared-pair debt + inter-packet gap bypass + LRU) on the re-inject
datapath. Whole-node network blast radius — stage reversibly.
