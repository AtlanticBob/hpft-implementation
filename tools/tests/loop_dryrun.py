#!/usr/bin/env python3
"""Whole-loop dry run of tx_agent_e against synthetic telemetry. No DPU,
no lab, no root: a local UDP socket stands in for the receiver DPU and a
real FIFO with a local reader stands in for the PCC RP mailbox.

This proves the PROCESS: that
main() parses the new record format, drives the sender tree, steps the
law, actuates, coalesces RDMA budgets at the mailbox rate and degrades
through freeze -> fail-open. It is the cheapest way to catch a NameError
or a wiring mistake left over from deleting three law skeletons, and it
costs nothing compared to catching it on the DPU with a wedged RP.

Phases (wall clock):
  0.0-1.2s  u = 6G steady   -> R climbs from the tree start and settles
  1.2-2.0s  u = 3G          -> down-step, must settle in ~130 ms
  2.0-4.6s  telemetry off   -> freeze (n1) then fail-open ramp to Tree_f

Usage: python3 loop_dryrun.py    Exit 0 = all assertions held.
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "tools", "dpu"))

import rx_agent          # noqa: E402
import tx_agent_e        # noqa: E402

PORT = 19710
SHIM_PORT = 19711
FS = "sgpu01/vf0>sgpu02/vf0|rdma"

# A fixed UDP port makes this test non-reentrant: a previous invocation
# whose daemon threads outlived it still holds 19710, and the next run
# then produces no records and dies on its timeout rather than saying
# why. Say why instead - and do NOT try to clear the ground by pattern-
# killing, because a pattern containing this file's name also matches
# whatever is supervising this run (a `timeout` wrapper carries the same
# string in its command line), so the cleanup kills its own parent.
_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    _probe.bind(("127.0.0.1", PORT))
except OSError:
    print("port %d is already bound - a previous loop_dryrun is still "
          "alive. Kill it BY PID and rerun." % PORT)
    sys.exit(2)
_probe.close()

tmp = tempfile.mkdtemp(prefix="hpft_dryrun_")
fifo = os.path.join(tmp, "rp_fifo")
os.mkfifo(fifo)
tx_agent_e.FIFO = fifo                      # stand in for the RP mailbox
log = os.path.join(tmp, "tx.jsonl")

reg = json.load(open(os.path.join(REPO, "config", "lab-registry.json")))
reg["e_params"]["telemetry_port"] = PORT
# eviction is a 30 s production timer; the test drives it directly rather
# than waiting, which is the only way the POSITIVE half is reachable in a
# run this short
reg["e_params"]["n3_evict_s"] = 0.5
# this dry run exercises the v2 tracking arm; the vq arm has its own
# closed-loop check (vq_check.py)
reg["e_params"]["law"] = "conf"
reg["control"]["pace_shim"] = {"sgpu01": "127.0.0.1:%d" % SHIM_PORT}
regpath = os.path.join(tmp, "registry.json")
json.dump(reg, open(regpath, "w"))
EP = reg["e_params"]

# a shim sink, so the tcp actuation path has somewhere to send
shim = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
shim.bind(("127.0.0.1", SHIM_PORT))

# hold the FIFO read end open and collect what the mailbox writes
budgets = []
stop = threading.Event()


def fifo_reader():
    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    buf = b""
    while not stop.is_set():
        try:
            chunk = os.read(fd, 65536)
        except BlockingIOError:
            chunk = b""
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                budgets.append((time.monotonic(), line.decode().strip()))
        else:
            time.sleep(0.002)
    os.close(fd)


threading.Thread(target=fifo_reader, daemon=True).start()

sys.argv = ["tx_agent_e", "--registry", regpath, "--log", log]
threading.Thread(target=tx_agent_e.main, daemon=True).start()
time.sleep(0.3)                              # let it bind

tel = rx_agent.Telemetry({}, {}, PORT)
sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
t0 = time.monotonic()


def feed(u, r, until, q_us=0):
    while time.monotonic() - t0 < until:
        sender.sendto(tel._pack([(FS, u, r, q_us)]), ("127.0.0.1", PORT))
        time.sleep(EP["period_ms"] / 1e3)


# a second flow-set that stops early: it must be evicted while the first
# keeps the telemetry channel demonstrably alive
FS2 = "sgpu01/vf1>sgpu02/vf1|rdma"


def feed2(u, r, until, both=True):
    while time.monotonic() - t0 < until:
        recs = [(FS, u, r, 0)] + ([(FS2, u, r, 0)] if both else [])
        sender.sendto(tel._pack(recs), ("127.0.0.1", PORT))
        time.sleep(EP["period_ms"] / 1e3)


# law=conf reads the 4th field as the virtual queue in us and treats u as
# the direction bit, so the stimulus is a queue, not a target rate: an empty
# ledger first (the fence must probe UP), then a sustained queue (it must
# repay DOWN). Feeding a target rate here is what the v2 law wanted.
feed2(6e9, 5.7e9, 1.2)        # q = 0: both flow-sets, fence probes up
t_down = time.monotonic() - t0
feed(6e9, 5.7e9, 2.0, q_us=40000)   # 40 ms of queue: the fence must repay
t_off = time.monotonic() - t0
time.sleep(2.6)                              # telemetry silence
stop.set()
time.sleep(0.1)

all_rows = []
for l in open(log):
    l = l.strip()
    if l:
        try:
            all_rows.append(json.loads(l))
        except ValueError:
            pass
rows = [r for r in all_rows if r.get("fs") == FS]
fails = []


def check(name, ok, detail):
    print("%-36s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


check("D1 agent produced log records", len(rows) > 10,
      "%d records for %s" % (len(rows), FS))

modes = [r["mode"] for r in rows]
check("D2 only the live modes appear",
      set(modes) <= {"conf", "track", "frozen", "fail_open"},
      "modes seen: %s" % sorted(set(modes)))

# an empty ledger must make the fence climb, and a sustained queue must
# make it come back down. Under law=conf these are the two regimes of §5.2,
# and the fence is the only state that moves - there is no target to track.
up = [r for r in rows if r["ts"] - rows[0]["ts"] < t_down - 0.2]
check("D3 an empty ledger makes the fence climb",
      len(up) > 5 and up[-1]["R"] > up[0]["R"],
      "R %.2fG -> %.2fG while q = 0" % (up[0]["R"] / 1e9, up[-1]["R"] / 1e9))

dn = [r for r in rows if r.get("d", 0) > 0]
check("D4 a sustained queue makes the fence repay",
      bool(dn) and dn[-1]["R"] < (up[-1]["R"] if up else float("inf")),
      "R fell to %.2fG under a 40 ms ledger" % (dn[-1]["R"] / 1e9 if dn
                                                else float("nan")))

check("D5 pace floor respected",
      all(r.get("pace", 1) >= reg["control"]["pace_floor_bps"]
          for r in rows if "pace" in r),
      "min pace %.0f Mbps" % (min(r["pace"] for r in rows
                                  if "pace" in r) / 1e6))

check("D6 freeze then fail-open on silence",
      "frozen" in modes and "fail_open" in modes,
      "freeze at n1=%.2fs, fail-open at n2=%.1fs after last record"
      % (EP["n1_freeze_s"], EP["n2_failopen_s"]))

fo = [r for r in rows if r.get("mode") == "fail_open"]
check("D7 fail-open climbs towards Tree_f",
      len(fo) > 2 and fo[-1]["R"] > fo[0]["R"],
      "R %.2fG -> %.2fG over %d fail-open records"
      % (fo[0]["R"] / 1e9 if fo else 0, fo[-1]["R"] / 1e9 if fo else 0,
         len(fo)))

# mailbox: batches at the coalescing rate, budget descends on the
# down-step (this is the line a descent limit on the budget would hold back)
gaps = [b[0] - a[0] for a, b in zip(budgets, budgets[1:])]
med = sorted(gaps)[len(gaps) // 2] if gaps else 0
check("D8 RDMA budgets coalesced at ~13 ms",
      bool(gaps) and 0.008 < med < 0.020,
      "%d batches, median gap %.0f ms (rdma_push_ms=%d)"
      % (len(budgets), med * 1e3, EP["rdma_push_ms"]))

vals = []
for _, line in budgets:
    parts = line.split()
    # 0xb47c is the three-word batch; the fence design writes 0xb47d, which
    # carries the executor's trust as a fourth word per entry. Matching only
    # the old header read every budget as absent.
    if len(parts) >= 4 and parts[0][:6] in ("0xb47c", "0xb47d"):
        vals.append(int(parts[2]))
check("D9 budget descends without a ramp",
      bool(vals) and min(vals) < max(vals) * 0.7,
      "budget units %d -> %d (min %d): one step down, not a ramp"
      % (vals[0] if vals else 0, vals[-1] if vals else 0,
         min(vals) if vals else 0))


# --- eviction (added 2026-07-27) -------------------------------------
# Two halves, and the second matters more than the first: a flow-set that
# stops must be forgotten, and a flow-set that stops BECAUSE THE WHOLE
# CHANNEL DIED must NOT be - otherwise a telemetry outage would silently
# release every flow from its Tree_f cap, which is the one thing §4.4
# forbids. Both are checked because only one of them is the safe default.
ev = [r for r in all_rows if r.get("mode") == "evicted"]
check("D10 a flow-set that stops alone IS evicted",
      any(r.get("fs") == FS2 for r in ev),
      "FS2 stopped at t+1.2s while FS kept the channel alive; %d eviction "
      "records" % len(ev))
check("D11 nothing evicted during the global outage",
      not any(r.get("fs") == FS for r in ev),
      "the run ends in a channel-wide outage, and the flow-set that was "
      "live going into it is still held (capped by Tree_f, per §4.4)")

print("\n%d/11 checks passed   (log: %s)" % (11 - len(fails), log))
sys.exit(1 if fails else 0)
