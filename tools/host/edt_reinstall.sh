#!/usr/bin/env bash
# Reinstall the host fq+edt TCP executor on THIS host after the BPF program
# changed: tear every VF's old filter down (a re-apply on top of an old
# filter leaves the old program shaping from its own unpinned maps), unpin,
# load and attach the new program, repopulate its helper maps, and restart
# the pace shim so it seeds pair_state at the new size. Idempotent.
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
cd "$REPO" || exit 1
B=${HPFT_BPFTOOL:-$(ls -1 /usr/lib/linux-tools-*/bpftool 2>/dev/null | sort -V | tail -1)}
B=${B:-$(command -v bpftool)}
for d in $(ls /sys/class/net | grep -E '^dpu1vf[0-9]+$'); do
    sudo tc filter del dev "$d" egress 2>/dev/null
done
sudo rm -rf /sys/fs/bpf/hpft_tcp_edt
sudo HPFT_BPFTOOL=$B python3 tools/tcp_shaper/tools/tcp-shaper-apply \
    --registry config/lab-tcp-registry.json --local-host "$(hostname)" \
    --bpftool "$B" --egress-dev dpu1vf0 --apply >/tmp/edt_apply.json 2>&1 \
    || echo "  WARN $(hostname): tcp-shaper-apply reported failure (fq on parent 0:1 is expected to fail)"
sudo bash tools/host/edt_ensure.sh | sed "s/^/  $(hostname): /"
bash tools/host/edt_maps_ensure.sh | sed "s/^/  $(hostname): /"
if systemctl is-active hpft-pace-shim >/dev/null 2>&1; then
    sudo systemctl restart hpft-pace-shim
    echo "  $(hostname): pace shim restarted"
fi
sz=$(sudo "$B" map show pinned /sys/fs/bpf/hpft_tcp_edt/maps/hpft_pair_state 2>/dev/null | grep -o 'value [0-9]*' | awk '{print $2}')
echo "  $(hostname): hpft_pair_state value ${sz}B"
