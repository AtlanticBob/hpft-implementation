# Jakiro on this testbed: the one adaptation, and why

The Jakiro baseline is the authors' DHTB implementation, used as it is: the
token-bucket shape, the borrow rule, the greedy decision, the CE marking of
RoCE and the dropping of TCP are all untouched. One thing had to change before
it could be run here at all, and it is a change to what the classifier *sees*,
not to what it *decides*.

**The gate matched one peer.** Jakiro's root pipe is an exact match on the
outer VxLAN header: ingress port, outer source address, outer destination
address, VxLAN destination port and VNI. The source address came from a single
configuration value, so the pipeline accepted tunnelled traffic from exactly
one peer. That is enough for the two-machine testbed the code was written on.
This lab has four machines and every scenario that is worth putting Jakiro in
has two or three senders reaching the same receiver, so traffic from all but
one of them would miss the gate, never reach the DHTB, and arrive at the VF
unpoliced. The arm would report no policing and look like a Jakiro failure
when it was our topology the code had never seen.

**What changed.** `UNDERLAY_SRC_IP` now takes a comma-separated list and
`add_outer_vxlan_gate_entry` installs one entry per address, each the same
exact match the original built. `jakiro_multipeer.patch` is the diff.
`jakiro_conf.sh` writes the list.

**What did not.** The meters, the colour pipes, the root/leaf hierarchy and the
greedy decision are the authors' code. The known platform limitation the
authors' own README records - `switch,hws` rejects the outer-to-inner ECN
inheritance descriptor, so that step is not installed - still stands and is not
something this patch touches.
