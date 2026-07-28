#!/usr/bin/env python3
"""Offline checks for the §4.3 hardware layer, chiefly its units.

This module's whole risk is silent: `devlink` takes bits/s on the command
line and reports bytes/s in JSON, so an inverted conversion produces a cap
that is 8x wrong in either direction with no error anywhere. The first
deployment did exactly that - asked for 30G, enforced 3.75G - and only a
readback caught it. These checks pin the round trip against a stub so the
next edit cannot reintroduce it without failing here.

usage: hw_maxrate_test.py    exit 0 = all checks passed
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hw_maxrate  # noqa: E402

fails = []


def check(name, ok, detail=""):
    print("%-50s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
    if not ok:
        fails.append(name)


PORTS = ('{"port":{"pci/0000:03:00.1/262145":{"netdev":"pf1vf0"},'
         '"pci/0000:03:00.1/262146":{"netdev":"pf1vf1"}}}')
VNICS = [{"vnic_id": "h/vf0", "host": "h", "representor": "pf1vf0"},
         {"vnic_id": "h/vf1", "host": "h", "representor": "pf1vf1"},
         {"vnic_id": "other/vf0", "host": "other", "representor": "pf1vf0"}]

writes = []
hw_bytes = {}       # handle -> tx_max as the "hardware" holds it (bytes/s)


def fake_run(argv, timeout=10):
    if argv[:3] == ["devlink", "port", "show"]:
        return PORTS
    if argv[:5] == ["devlink", "port", "function", "rate", "show"]:
        h = argv[5]
        return ('{"rate":{"%s":{"type":"leaf","tx_max":%d}}}'
                % (h, hw_bytes.get(h, 0)))
    if argv[:5] == ["devlink", "port", "function", "rate", "set"]:
        h, val = argv[5], int(argv[7])
        writes.append((h, val))
        hw_bytes[h] = val // 8      # the CLI takes bits, the NIC holds bytes
        return ""
    raise AssertionError("unexpected: %s" % argv)


hw_maxrate._run = fake_run

hw = hw_maxrate.HwMaxRate("h", VNICS)
hw.discover()
check("A1 only this host's vnics are bound", set(hw.reps) == {"h/vf0", "h/vf1"},
      "other/vf0 belongs to another DPU")
check("A2 representors resolve to rate leaves",
      hw.handles.get("h/vf0", "").endswith("262145") and hw.error is None)

changed, errors = hw.sync({"h/vf0": 30_000_000_000})
check("B1 the CLI is given BITS/s", writes and writes[-1][1] == 30_000_000_000,
      "wrote tx_max %d" % (writes[-1][1] if writes else -1))
check("B2 readback converts BYTES/s back to bits",
      hw.current()["h/vf0"] == 30_000_000_000,
      "hardware holds %d bytes/s -> %.1fG"
      % (hw_bytes[hw.handles["h/vf0"]], hw.current()["h/vf0"] / 1e9))
check("B3 no errors on the happy path", not errors and changed)

n = len(writes)
hw.sync({"h/vf0": 30_000_000_000})
check("C1 an unchanged cap is not rewritten", len(writes) == n,
      "a reload must not reprogram the vport scheduler for nothing")
hw.sync({"h/vf0": 20_000_000_000})
check("C2 a changed cap is written", len(writes) == n + 1
      and writes[-1][1] == 20_000_000_000)

hw.clear()
check("D1 clear releases every bound leaf",
      all(v == 0 for v in hw.current().values()), "tx_max 0 = unlimited")

pol = {"vms": {"h/vf0": {"max_rate_bps": 30e9}, "h/vf1": {"weight": 1}}}
caps = hw_maxrate.caps_from_policy(pol, VNICS, "h")
check("E1 a VM with no MaxRate is left alone",
      caps == {"h/vf0": 30_000_000_000},
      "unsold and sold-everything are different states")


def boom(argv, timeout=10):
    raise RuntimeError("devlink: command not found")


hw_maxrate._run = boom
hw2 = hw_maxrate.HwMaxRate("h", VNICS)
hw2.discover()
check("F1 a DPU without devlink degrades, not crashes",
      hw2.handles == {} and hw2.error,
      "layer two still enforces; the loss is the backstop, not the pacing")

print("\n%d/%d checks passed" % (10 - len(fails), 10))
sys.exit(1 if fails else 0)
