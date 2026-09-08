#!/usr/bin/env python3
"""Insert measured RDMA flowtag rows into the registry.

Reads "src/vfS>dst/vfD 0x........" lines (flowtag_probe.sh output) on
stdin and adds them to rdma_flowtags in config/lab-registry.json, keeping
the file's formatting. A row that already exists with a different value is
an error: tags are stable across QP recreation, IP choice and reflash, so
a changed reading means the probe method was wrong, not the card.
usage: flowtag_probe.sh sgpu03 sgpu04 | registry_flowtags.py
"""
import json, re, sys
P = "config/lab-registry.json"
s = open(P).read()
reg = json.loads(s)
have = reg["rdma_flowtags"]
rows = []
for line in sys.stdin:
    m = re.match(r"(\S+/vf\d+>\S+/vf\d+)\s+(0x[0-9a-f]{1,8})\s*$", line.strip())
    if not m:
        continue
    k, v = m.group(1), m.group(2)
    if k in have and have[k].lower() != v.lower():
        sys.exit("registry_flowtags: %s is %s in the registry, probe read %s" % (k, have[k], v))
    if k not in have:
        rows.append((k, v))
if not rows:
    print("registry_flowtags: nothing new"); sys.exit(0)
# insert before the closing brace of the rdma_flowtags object, matching the
# indentation of its last row
m = re.search(r'("rdma_flowtags": \{.*?)(\n(\s*)\}\s*,)', s, re.S)
assert m, "rdma_flowtags block not found"
body, close, ind = m.group(1), m.group(2), m.group(3)
last_ind = re.findall(r'\n(\s*)"[^"]+": "0x', body)[-1]
add = "".join(',\n%s"%s": "%s"' % (last_ind, k, v) for k, v in rows)
s = s[:m.start(2)] + add + s[m.start(2):]
json.loads(s)
open(P, "w").write(s)
print("registry_flowtags: added %d rows: %s" % (len(rows), ", ".join(k for k, _ in rows)))
