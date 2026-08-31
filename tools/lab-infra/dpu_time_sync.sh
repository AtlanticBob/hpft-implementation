#!/usr/bin/env bash
# Sync every DPU's clock to its host over tmfifo.
#
# The DPUs have no time source (no chrony, no route to the site NTP server);
# their clocks were hundreds of ms apart and drifting, which corrupts every
# comparison of rx_agent vs tx_agent timestamps (found 2026-08-29). Each host
# runs a tiny SNTP responder (tools/lab-infra/sntp_server.py, transient unit
# hpft-sntp) on its tmfifo address; each DPU's systemd-timesyncd is pointed
# (tools/dpu/sntp_client.py, transient unit hpft-timesync, adjtimex
# ADJ_SETOFFSET every 8 s; the DPU image has neither timesyncd nor chrony).
# Hosts themselves stay on the site NTP.
#
#   dpu_time_sync.sh apply     start responders, configure + restart timesyncd on the DPUs
#   dpu_time_sync.sh status    offset / RTT as timesyncd sees it, per DPU
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
CMD=${1:-status}
NODES=$(python3 -c "
import json;r=json.load(open('$REPO/config/lab-registry.json'))
print(' '.join('%s,%s,%s'%(n['host'],n['dpu'],n['tmfifo_host']) for n in r['nodes']))")
on_host() { if [ "$1" = "$(hostname)" ]; then shift; bash -c "$*"; else h=$1; shift; ssh -n -o BatchMode=yes "$h" "$*" </dev/null; fi; }
for n in $NODES; do
  IFS=, read -r host dpu tmf <<<"$n"
  case "$CMD" in
    apply)
      on_host "$host" "sudo systemctl stop hpft-sntp 2>/dev/null; sudo systemctl reset-failed hpft-sntp 2>/dev/null; sudo systemd-run --unit hpft-sntp --property=Restart=always /usr/bin/python3 $REPO/tools/lab-infra/sntp_server.py 0.0.0.0 123 >/dev/null 2>&1 && echo '  $host: sntp responder up'"
      scp -q "$REPO/tools/dpu/sntp_client.py" "$dpu:/tmp/sntp_client.py"
      ssh -n -o BatchMode=yes "$dpu" "sudo systemctl stop hpft-timesync 2>/dev/null; sudo systemctl reset-failed hpft-timesync 2>/dev/null; sudo systemd-run --unit hpft-timesync --property=Restart=always /usr/bin/python3 /tmp/sntp_client.py $tmf 2 >/dev/null 2>&1 && echo '  $dpu: sntp client -> $tmf'" </dev/null ;;
    status)
      ssh -n -o BatchMode=yes "$dpu" "printf '  %-10s ' $dpu; sudo journalctl -u hpft-timesync --no-pager -n 1 -o cat 2>/dev/null" </dev/null ;;
  esac
done
