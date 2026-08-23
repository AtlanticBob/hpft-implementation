#!/usr/bin/env bash
# ns/packet of the TCP shaper program, measured across one run.
# usage: bpf_cost.sh <label>   (prints before, run, after)
B=${HPFT_BPFTOOL:-/usr/lib/linux-tools-5.15.0-185/bpftool}
id=$(sudo tc filter show dev dpu1vf0 egress | grep -o "id [0-9]*" | head -1 | cut -d' ' -f2)
sudo $B prog show id "$id" | grep -o "run_time_ns [0-9]* run_cnt [0-9]*"
