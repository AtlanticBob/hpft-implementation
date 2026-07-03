#!/usr/bin/env bash
# Post-reflash DPU restore, run ON THE DPU (ubuntu user, passwordless sudo).
# Rebuilds the hpft OVS underlay topology recorded in
# backup/dpu-pre-reflash-20260703/ovs-vsctl-show.txt and re-creates work dirs.
set -eux

# default bridges from the fresh BFB image get removed
for br in $(sudo ovs-vsctl list-br); do
    case "$br" in
        underlay-p0|underlay-p1) ;;
        *) sudo ovs-vsctl del-br "$br" ;;
    esac
done

sudo ovs-vsctl --may-exist add-br underlay-p0
sudo ovs-vsctl --may-exist add-port underlay-p0 p0
sudo ovs-vsctl --may-exist add-port underlay-p0 pf0hpf
sudo ip link set p0 up
sudo ip link set underlay-p0 up

sudo ovs-vsctl --may-exist add-br underlay-p1
sudo ovs-vsctl --may-exist add-port underlay-p1 p1
sudo ovs-vsctl --may-exist add-port underlay-p1 pf1hpf
for i in 0 1 2 3; do
    sudo ovs-vsctl --may-exist add-port underlay-p1 pf1vf$i || true
    sudo ip link set pf1vf$i up 2>/dev/null || true
done
sudo ip link set p1 up
sudo ip link set underlay-p1 up

mkdir -p ~/bzx
sudo ovs-vsctl show
