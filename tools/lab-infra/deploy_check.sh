#!/usr/bin/env bash
# Is what runs on the DPUs what is in the repo?
#
# Written after losing a lab run to the answer being no: tx_agent_e.py was
# deployed to the sender while the fastfill.py it had just started
# importing was not, so the sender agent crash-looped and RDMA ran
# completely unpaced. Every layer above looked healthy - the RP was up,
# the receiver was fine, the experiment produced a full set of numbers -
# and those numbers were meaningless. A crashed agent is not a silent
# failure mode anyone should have to notice by hand.
#
# SYMMETRIC (2026-08-23, four-node lab): every DPU gets the SAME payload -
# both planes, both executors. Which plane runs is a per-experiment role,
# not a per-node property, so a per-role file list would only be a second
# place for the lab's shape to drift from the registry's. The node list
# comes from config/lab-registry.json "nodes": adding a node there is the
# only edit needed to pull it into the sync loop.
#
# One ssh round trip per node for all hashes and one for all health, nodes
# in parallel: the old per-file ssh was 12 sequential round trips for two
# nodes, which at four would be 32 and slow enough that people skip it.
# A check that is skipped is a check that does not exist.
#
# usage: deploy_check.sh [--deploy]   (--deploy pushes what differs)
set -u
REPO=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO"

# Code mismatch is FATAL, configuration mismatch is a WARNING. Every
# runner pushes its own scenario registry, so a strict registry check
# makes chained runs impossible - and the failure it would be guarding
# against is not the same kind: the wrong registry produces a
# well-defined experiment with the wrong parameters, which the run's own
# output shows, whereas the wrong code produces a crashed agent and
# plausible numbers that show nothing.
DPU_FILES="tools/dpu/rx_agent.py tools/dpu/tx_agent_e.py tools/dpu/fastfill.py
           tools/dpu/fastfill.c tools/dpu/hw_maxrate.py tools/dpu/vport_meter.c
           tools/dpu/vhca_of.c
           tools/dpu/systemd/hpft-vport-meter.service tools/dpu/rp_service.sh"
DPU_FILES=$(echo $DPU_FILES)
CFG_FILE=config/lab-registry.json
# The executors are not Python and were long not covered here, which is
# exactly backwards: the policy plane fails loudly, an executor built from
# stale source fails silently at full plausibility.
PCC_SRC=tools/dpu/pcc/rp_rtt_template_dev_main.c
PCC_DEV=/home/ubuntu/bzx/doca34-apps/pcc/device/rp/rtt_template/rp_rtt_template_dev_main.c
PCC_HSRC=tools/dpu/pcc/pcc_host.c
PCC_HOST=/home/ubuntu/bzx/doca34-apps/pcc/host/pcc.c
PCC_BUILD=/home/ubuntu/bzx/doca34-apps

NODES=$(python3 -c "
import json;print(' '.join(n['dpu'] for n in json.load(open('$CFG_FILE'))['nodes']))")
HOSTS=$(python3 -c "
import json;print(' '.join(n['host'] for n in json.load(open('$CFG_FILE'))['nodes']))")
[ -n "$NODES" ] || { echo "deploy_check: no nodes in $CFG_FILE"; exit 1; }

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
bad=0; warn=0

remote_paths() { for f in $DPU_FILES $CFG_FILE; do echo -n "/opt/hpft/$(basename "$f") "; done; echo -n "$PCC_DEV $PCC_HOST"; }

# ---- one round trip per node, all nodes at once -------------------------
for n in $NODES; do
  ( ssh -o BatchMode=yes "$n" "md5sum $(remote_paths) 2>/dev/null" > "$TMP/$n.md5" 2>/dev/null ) &
done
wait

md5_from() { # md5_from <md5sum-output-file> <path-as-listed>
  awk -v p="$2" '$2==p{print $1}' "$1"
}

want_of() { md5sum "$1" | cut -d' ' -f1; }
have_of() { # have_of <node> <remote-path>
  awk -v p="$2" '$2==p{print $1}' "$TMP/$1.md5"
}

for n in $NODES; do
  push=""
  for f in $DPU_FILES; do
    [ "$(want_of "$f")" = "$(have_of "$n" "/opt/hpft/$(basename "$f")")" ] && continue
    echo "  DIFFERS  $n:/opt/hpft/$(basename "$f")"; push="$push $f"; bad=1
  done
  if [ "$(want_of $CFG_FILE)" != "$(have_of "$n" /opt/hpft/$(basename $CFG_FILE))" ]; then
    echo "  DIFFERS  $n:/opt/hpft/$(basename $CFG_FILE)  (config: warning)"; warn=1
    push="$push $CFG_FILE"
  fi
  if [ "$(want_of $PCC_SRC)" != "$(have_of "$n" "$PCC_DEV")" ]; then
    echo "  DIFFERS  $n:$PCC_DEV  (rebuild: meson setup --reconfigure build && ninja -C build pcc/doca_pcc)"
    bad=1; push="$push $PCC_SRC"
  fi
  if [ "$(want_of $PCC_HSRC)" != "$(have_of "$n" "$PCC_HOST")" ]; then
    echo "  DIFFERS  $n:$PCC_HOST  (rebuild: ninja -C build pcc/doca_pcc)"
    bad=1; push="$push $PCC_HSRC"
  fi
  echo "$push" > "$TMP/$n.push"
done

# ---- push what differs, rebuild only what the push invalidated ----------
if [ "${1:-}" = "--deploy" ] && { [ $bad -ne 0 ] || [ $warn -ne 0 ]; }; then
  echo "== deploying =="
  for n in $NODES; do
    push=$(cat "$TMP/$n.push")
    [ -n "${push// /}" ] || continue
    (
      for f in $push; do
        case "$f" in
          "$PCC_SRC") scp -q "$f" "$n:$PCC_DEV" ;;
          "$PCC_HSRC") scp -q "$f" "$n:$PCC_HOST" ;;
          *)          scp -q "$f" "$n:/opt/hpft/" ;;
        esac
      done
      # Both native artefacts are per-architecture: build them on the Arm
      # rather than shipping the x86 objects built for the benchmarks.
      case " $push " in *" tools/dpu/fastfill.c "*)
        ssh -o BatchMode=yes "$n" 'cd /opt/hpft && gcc -O2 -shared -fPIC -o libfastfill.so fastfill.c -lm' \
          || echo "  WARN $n: libfastfill.so build failed (agent falls back to Python)" ;;
      esac
      case " $push " in *" tools/dpu/vport_meter.c "*)
        ssh -o BatchMode=yes "$n" 'cd /opt/hpft && gcc -O2 -o vport_meter vport_meter.c -libverbs -lmlx5' \
          || echo "  WARN $n: vport_meter build failed (rx falls back to its old attribution chain)" ;;
      esac
      case " $push " in *" tools/dpu/vhca_of.c "*)
        ssh -o BatchMode=yes "$n" 'cd /opt/hpft && gcc -O2 -o vhca_of vhca_of.c -libverbs -lmlx5' \
          || echo "  WARN $n: vhca_of build failed (tx agent keys QP bindings by bare qpn)" ;;
      esac
      case " $push " in *" $PCC_SRC "*)
        ssh -o BatchMode=yes "$n" "cd $PCC_BUILD && meson setup --reconfigure build >/dev/null 2>&1 && ninja -C build pcc/doca_pcc >/dev/null 2>&1" \
          || echo "  WARN $n: doca_pcc rebuild failed - the RUNNING executor is still the old device code" ;;
      esac
      case " $push " in *" $PCC_HSRC "*)
        case " $push " in *" $PCC_SRC "*) ;; *)
          ssh -o BatchMode=yes "$n" "cd $PCC_BUILD && ninja -C build pcc/doca_pcc >/dev/null 2>&1" \
            || echo "  WARN $n: doca_pcc host rebuild failed" ;;
        esac ;;
      esac
    ) &
  done
  wait
  # re-read and re-compare: a deploy that reports success without looking
  # again is the same trust-without-checking this tool exists to remove.
  bad=0; warn=0
  for n in $NODES; do
    ( ssh -o BatchMode=yes "$n" "md5sum $(remote_paths) 2>/dev/null" > "$TMP/$n.md5" 2>/dev/null ) &
  done
  wait
  for n in $NODES; do
    for f in $DPU_FILES; do
      [ "$(want_of "$f")" = "$(have_of "$n" "/opt/hpft/$(basename "$f")")" ] || { echo "  STILL DIFFERS  $n:$(basename "$f")"; bad=1; }
    done
    [ "$(want_of $PCC_SRC)" = "$(have_of "$n" "$PCC_DEV")" ] || { echo "  STILL DIFFERS  $n:$PCC_DEV"; bad=1; }
    [ "$(want_of $PCC_HSRC)" = "$(have_of "$n" "$PCC_HOST")" ] || { echo "  STILL DIFFERS  $n:$PCC_HOST"; bad=1; }
  done
fi

# ---- health: an agent that is not running is the same class of problem --
# Scoped to units that are ENABLED. Every node carries both units, so
# "installed" says nothing about what this experiment wants running;
# "enabled" is the node's own statement of its role, and a role a node
# claims but does not fulfil is exactly the silent failure above.
for n in $NODES; do
  ( ssh -o BatchMode=yes "$n" '
      for u in hpft-rxagent-e hpft-txagent-e hpft-vport-meter; do
        en=$(systemctl is-enabled $u 2>/dev/null)
        st=$(systemctl is-active $u 2>/dev/null)
        # A unit is in scope if the node claims it either way: installed and
        # enabled, or running right now. `is-enabled` EXITS NON-ZERO for a
        # transient unit, so gating on its exit status alone silently skipped
        # every agent roles.sh starts - the tx agents were going unchecked,
        # which is the exact hole this file was written to close. `failed`
        # stays in scope on purpose: a failed agent is the finding.
        case "$en" in enabled|enabled-runtime|static|transient) ;; *)
          case "$st" in active|activating|failed) ;; *) continue ;; esac ;;
        esac
        echo "UNIT $u $st"
        # ... and one that is up but crash-looping reports active only
        # briefly. Scope this to the recent past, not the whole journal: a
        # traceback from a fault already fixed is history, and a check that
        # cries wolf about history is a check people learn to ignore.
        echo "TB $u $(sudo journalctl -u $u --no-pager -o cat --since "-90s" 2>/dev/null | grep -ac Traceback)"
      done' > "$TMP/$n.health" 2>/dev/null ) &
done
wait
for n in $NODES; do
  [ -s "$TMP/$n.health" ] || { echo "  NO UNITS ENABLED  $n (node carries the code but claims no role)"; warn=1; continue; }
  while read -r kind u v; do
    case "$kind" in
      UNIT) [ "$v" = active ] || { echo "  NOT ACTIVE  $n/$u ($v)"; bad=1; } ;;
      TB)   [ "${v:-0}" -eq 0 ] || { echo "  TRACEBACKS  $n/$u: $v in the last 90 s"; bad=1; } ;;
    esac
  done < "$TMP/$n.health"
done

# ---- the host plane -----------------------------------------------------
# The pace shim, the TCP shaper library and the BPF object run on the sender
# HOST, not its DPU, and they resolve each other by absolute repo path. So
# the payload is mirrored at that SAME absolute path on every host rather
# than parked somewhere neutral: a neutral directory would mean editing the
# paths, and a path edited in one file and not another is the drift this
# tool exists to end. Only sgpu01 holds the git checkout; the others hold
# exactly the files below, kept identical to it.
HOST_FILES="tools/host/hpft_pace_shim.py tools/host/qpn_resolver.py
            tools/host/edt_ensure.sh tools/host/edt_maps_ensure.sh tools/host/edt_reinstall.sh
            tools/lab-infra/vf_setup.sh tools/cross_pair_net.sh
            tools/tcp_shaper/tools/tcp_shaper_lib.py tools/tcp_shaper/tools/tcp-shaper-apply
            tcp/bpf-opt3/hpft_tcp_edt_kern.o tcp/bpf-opt3/hpft_tcp_edt_kern.c
            tcp/bpf-opt3/hpft_bpf_helpers.h tools/dpu-timesync.sh
            tools/host/udp_blast.c
            config/lab-tcp-registry.json config/lab-registry.json"
HOST_FILES=$(echo $HOST_FILES)
for h in $HOSTS; do
  [ "$h" = "$(hostname)" ] && continue
  ( ssh -o BatchMode=yes "$h" "cd $REPO 2>/dev/null && md5sum $HOST_FILES 2>/dev/null" > "$TMP/$h.hostmd5" 2>/dev/null ) &
done
wait
for h in $HOSTS; do
  [ "$h" = "$(hostname)" ] && continue
  hpush=""
  for f in $HOST_FILES; do
    have=$(md5_from "$TMP/$h.hostmd5" "$f")
    [ "$(want_of "$f")" = "$have" ] && continue
    echo "  DIFFERS  $h:$REPO/$f  (host plane)"; hpush="$hpush $f"; bad=1
  done
  echo "$hpush" > "$TMP/$h.hostpush"
done
if [ "${1:-}" = "--deploy" ]; then
  for h in $HOSTS; do
    [ "$h" = "$(hostname)" ] && continue
    hpush=$(cat "$TMP/$h.hostpush" 2>/dev/null)
    [ -n "${hpush// /}" ] || continue
    ( # joined onto one line: a newline in the argument makes the remote
      # shell read the second path as a new command, which fails silently
      # for every directory but the first.
      dirs=$(for f in $hpush; do dirname "$REPO/$f"; done | sort -u | tr "\n" " ")
      ssh -o BatchMode=yes "$h" "mkdir -p $dirs"
      for f in $hpush; do scp -q "$f" "$h:$REPO/$f"; done
      # the apply tool recompiles when the .c looks newer than the .o, and
      # scp stamps both with the copy time in whatever order they went. The
      # object we ship IS built from that source, and these hosts have no
      # clang, so make the ordering say so.
      ssh -o BatchMode=yes "$h" "test -e $REPO/tcp/bpf-opt3/hpft_tcp_edt_kern.o && touch $REPO/tcp/bpf-opt3/hpft_tcp_edt_kern.o" 2>/dev/null ) &
  done
  wait
  bad=0
  for h in $HOSTS; do
    [ "$h" = "$(hostname)" ] && continue
    ssh -o BatchMode=yes "$h" "cd $REPO && md5sum $HOST_FILES 2>/dev/null" > "$TMP/$h.hostmd5" 2>/dev/null
    for f in $HOST_FILES; do
      have=$(md5_from "$TMP/$h.hostmd5" "$f")
      [ "$(want_of "$f")" = "$have" ] || { echo "  STILL DIFFERS  $h:$f"; bad=1; }
    done
  done
fi

# ---- the traffic generators every host must agree on --------------------
# A generator that differs across hosts does not fail loudly: an iperf 3.9
# client cannot speak to a 3.20 server and dies with "unable to send control
# message", so the run completes, the analyzer reports on the flow-sets that
# did appear, and the missing third of the experiment looks like it was never
# asked for. Same for the perftest fork: absent on a host, that sender simply
# contributes nothing.
gen_sig() { # gen_sig <host> -> "<iperf3 version> <perftest present>"
  local c='echo "$(iperf3 --version 2>/dev/null | head -1) | $(test -x '"$HOME"'/hyperfront/perftest-enhanced/ib_write_bw && echo perftest-ok || echo perftest-MISSING)"'
  if [ "$1" = "$(hostname)" ]; then bash -c "$c"; else ssh -o BatchMode=yes "$1" "$c" 2>/dev/null; fi
}
ref=""
for h in $HOSTS; do
  sig=$(gen_sig "$h")
  case "$sig" in *MISSING*) echo "  MISSING  $h: perftest-enhanced not installed"; bad=1 ;; esac
  if [ -z "$ref" ]; then ref=$sig; refh=$h
  elif [ "$sig" != "$ref" ]; then
    echo "  DIFFERS  $h traffic generators: $sig"
    echo "           $refh has: $ref"; bad=1
  fi
done

# ---- the TCP executor, on every host that has it loaded ----------------
# The BPF program cannot be md5'd against a source, but its map layout is
# derived from that source, so a stale program shows up as the wrong value
# size. This is the failure that already happened once: a pin left in place
# makes `bpftool prog load` fail, tc keeps pointing at the old program, and
# every layer reports healthy.
# A sender host that is missing the qpn resolver overshoots by its QP count
# with every other layer reading correct, so it belongs with the executor
# checks, not in anyone's memory.
for h in $HOSTS; do
  if [ "$h" = "$(hostname)" ]; then tx_here=$(ssh -o BatchMode=yes "$(python3 -c "
import json;print({n['host']:n['dpu'] for n in json.load(open('$CFG_FILE'))['nodes']}['$h'])")" 'systemctl is-active hpft-txagent-e 2>/dev/null')
    qr=$(systemctl is-active hpft-qpn-resolver 2>/dev/null)
  else
    tx_here=$(ssh -o BatchMode=yes "$(python3 -c "
import json;print({n['host']:n['dpu'] for n in json.load(open('$CFG_FILE'))['nodes']}['$h'])")" 'systemctl is-active hpft-txagent-e 2>/dev/null')
    qr=$(ssh -o BatchMode=yes "$h" 'systemctl is-active hpft-qpn-resolver 2>/dev/null' 2>/dev/null)
  fi
  [ "$tx_here" = active ] || continue        # not a sender this run
  [ "$qr" = active ] || { echo "  NOT ACTIVE  $h/hpft-qpn-resolver (sender without it overshoots by its QP count)"; bad=1; }
done

# The QPN resolver on every host joins its QPs against every peer over ssh
# AS THE LOGIN USER. A host whose user cannot reach a peer produces an empty
# table and every one of its QPs then runs at the executor's unknown-flow
# allowance while every service reports active (sgpu03/sgpu04, 2026-09-07:
# "Permission denied (publickey)" since the all-senders layout of 09-04).
echo "== resolver peers (user ssh) =="
for h in $HOSTS; do
  peers=$(for p in $HOSTS; do [ "$p" != "$h" ] && echo -n "$p "; done)
  if [ "$h" = "$(hostname)" ]; then
    unreach=$(for p in $peers; do timeout 8 ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "$p" true >/dev/null 2>&1 || echo -n "$p "; done)
  else
    unreach=$(ssh -o BatchMode=yes "$h" "for p in $peers; do timeout 8 ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \$p true >/dev/null 2>&1 || echo -n \"\$p \"; done" 2>/dev/null)
  fi
  if [ -n "$unreach" ]; then echo "  DIFFERS  $h: user ssh to $unreach FAILS - its QPN resolver sends no bindings"; bad=1; else echo "  ok       $h -> all peers"; fi
done
PIN=/sys/fs/bpf/hpft_tcp_edt/maps/hpft_pair_state
want_sz=$(python3 -c "import sys; sys.path.insert(0,'tools/tcp_shaper/tools'); from tcp_shaper_lib import pack_pair_state; print(len(pack_pair_state()))")
for h in $HOSTS; do
  if [ "$h" = "$(hostname)" ]; then
    sudo test -e "$PIN" || { echo "  WARN  $h: TCP shaper not loaded"; warn=1; continue; }
    B=${HPFT_BPFTOOL:-$(ls -1 /usr/lib/linux-tools-*/bpftool 2>/dev/null | sort -V | tail -1)}; B=${B:-$(command -v bpftool)}
    have_sz=$(sudo "$B" map show pinned "$PIN" 2>/dev/null | grep -o 'value [0-9]*B' | grep -o '[0-9]*')
  else
    have_sz=$(ssh -o BatchMode=yes "$h" "sudo test -e $PIN && sudo \$(ls -1 /usr/lib/linux-tools-*/bpftool 2>/dev/null | sort -V | tail -1) map show pinned $PIN 2>/dev/null | grep -o 'value [0-9]*B' | grep -o '[0-9]*'" 2>/dev/null)
    [ -n "$have_sz" ] || { echo "  WARN  $h: TCP shaper not loaded"; warn=1; continue; }
  fi
  [ "$want_sz" = "$have_sz" ] || {
    echo "  DIFFERS  $h: loaded TCP shaper is stale (hpft_pair_state ${have_sz}B, source says ${want_sz}B)"
    echo "           rm -rf /sys/fs/bpf/hpft_tcp_edt, then re-apply (see README)"; bad=1; }
done

if [ $bad -eq 0 ]; then
  if [ $warn -ne 0 ]; then
    echo "deploy_check: code OK on $(echo $NODES | wc -w) nodes, enabled agents healthy; see warnings above"
  else
    echo "deploy_check: repo == $(echo $NODES | wc -w) DPUs, enabled agents healthy"
  fi
else
  echo "deploy_check: CODE MISMATCH (run with --deploy to push)"
fi
exit $bad
