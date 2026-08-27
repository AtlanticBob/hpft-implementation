#!/usr/bin/env bash
# mb.sh <dpu> <word0> [word1 ...]  -- one mailbox request to that DPU's RP,
# prints the HPFT_RSP line (if the request has a response).
D=$1; shift
ssh -n -o BatchMode=yes "$D" "n=\$(wc -l < /tmp/pcc_rp.log); echo '$*' > /tmp/rp_fifo; sleep 0.35; tail -n +\$((n+1)) /tmp/pcc_rp.log | grep -a HPFT_RSP | head -1" </dev/null
