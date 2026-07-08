#!/usr/bin/env bash
# DPU time discipline (host -> DPU push), because the BlueField Arm has no usable
# NTP path: no internet, host runs systemd-timesyncd (client-only, not serving),
# DPU has no sntp/ntpdate/chrony and timesyncd is removed. The host (sgpu01,
# tmfifo 192.168.102.1) is the only accurate reachable clock. We push the host's
# precise time over a WARM ssh ControlMaster connection (transit ~RTT/2 ~1ms, vs
# ~1-2s for a cold connect) so the residual offset is single-digit ms. Also writes
# the DPU RTC so a reboot doesn't jump back. Run from cron on the host.
#
# Usage: dpu-timesync.sh [ssh-host-alias] [comp_ms]  (default: hpft-dpu, 15ms)
# comp_ms compensates for the one-way push transit + remote `date -s` processing
# so the DPU isn't left systematically behind the host (measured ~15-25ms on the
# local tmfifo link). Tune per link; a small over/under just shifts the residual.
set -u
DPU="${1:-hpft-dpu}"
COMP_MS="${2:-15}"
CM="/tmp/dpu-timesync-${DPU}.sock"

# Warm the master connection (cold connect cost paid here, not on the set).
ssh -o ControlMaster=auto -o ControlPath="$CM" -o ControlPersist=20 \
    -o BatchMode=yes -o ConnectTimeout=8 "$DPU" true 2>/dev/null || exit 1

# Set over the warm connection: host time captured immediately before the remote
# `date -s`, plus a transit compensation, then persist to the RTC.
NOW=$(date -u +%s.%N)
TARGET=$(awk -v n="$NOW" -v c="$COMP_MS" 'BEGIN{printf "%.6f", n + c/1000.0}')
ssh -o ControlPath="$CM" -o BatchMode=yes "$DPU" \
    "sudo date -u -s @$TARGET >/dev/null 2>&1 && sudo hwclock --systohc 2>/dev/null" 2>/dev/null
