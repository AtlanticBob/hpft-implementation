#!/usr/bin/env python3
"""Fill the per-VF facts the kernel decides into both registries.

A VF's rdma device name, its MAC and its PCI address are not ours to choose:
the kernel numbers mlx5_N by probe order (different on every host), the DPU
firmware hands out the MAC, and the PF's virtfn links say where the VF sits
on the bus. Anything written by hand for those three fields is a guess that
happens to be right until the next rebuild. This reads all three from every
host over ssh and writes them into config/lab-registry.json (rdma_dev, mac)
and config/lab-tcp-registry.json (vf_pci_bdf, rdma_devs, gids). Run it after
vf_setup.sh on every host, then deploy_check.sh --deploy.

usage: registry_refresh.py [--check]   (--check: report differences, write nothing)
"""
import json
import socket
import subprocess
import sys

REPO = "/home/zhaoxiang/hyperfront/hpft-implementation"
PROBE = r'''
for d in /sys/class/net/dpu1vf*; do
  n=$(basename $d); pci=$(basename $(readlink -f $d/device))
  ib=$(ls $d/device/infiniband 2>/dev/null | head -1)
  gid=$(cat /sys/class/infiniband/$ib/ports/1/gids/3 2>/dev/null | tr -d :)
  echo "$n $pci $(cat $d/address) ${ib:-none} ${gid:-none}"
done'''


def probe(host):
    if host == socket.gethostname():
        out = subprocess.run(["bash", "-c", PROBE], capture_output=True, text=True).stdout
    else:
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", host, PROBE],
                             capture_output=True, text=True, timeout=30).stdout
    facts = {}
    for line in out.split("\n"):
        p = line.split()
        if len(p) == 5:
            facts[p[0]] = {"pci": p[1], "mac": p[2], "ib": p[3], "gid": p[4]}
    return facts


def main():
    check = "--check" in sys.argv
    reg = json.load(open(f"{REPO}/config/lab-registry.json"))
    tcp = json.load(open(f"{REPO}/config/lab-tcp-registry.json"))
    changed = 0
    for node in reg["nodes"]:
        host = node["host"]
        facts = probe(host)
        if not facts:
            print(f"{host}: no VFs visible (host down or vf_setup not run)")
            continue
        for v in reg["vnics"]:
            if v["host"] != host:
                continue
            f = facts.get(v["netdev"])
            if not f:
                print(f"{host}: {v['netdev']} missing on the host")
                continue
            for key, val in (("rdma_dev", f["ib"]), ("mac", f["mac"])):
                if v.get(key) != val:
                    print(f"{host}/{v['netdev']}: {key} {v.get(key)} -> {val}")
                    v[key] = val
                    changed += 1
        for t in tcp["vnics"]:
            if t["host"] != host:
                continue
            f = facts.get(f"dpu1vf{t['vf_index']}")
            if not f:
                continue
            want = {"vf_pci_bdf": f["pci"], "rdma_devs": [f["ib"]],
                    "gids": [f["gid"]] if f["gid"] != "none" else []}
            for key, val in want.items():
                if t.get(key) != val:
                    print(f"{host}/vf{t['vf_index']}: tcp-registry {key} {t.get(key)} -> {val}")
                    t[key] = val
                    changed += 1
    if check:
        print(f"{changed} field(s) differ" if changed else "registries match the hosts")
        return 1 if changed else 0
    if changed:
        json.dump(reg, open(f"{REPO}/config/lab-registry.json", "w"), indent=2, ensure_ascii=False)
        open(f"{REPO}/config/lab-registry.json", "a").write("\n")
        json.dump(tcp, open(f"{REPO}/config/lab-tcp-registry.json", "w"), indent=2)
        open(f"{REPO}/config/lab-tcp-registry.json", "a").write("\n")
    print(f"{changed} field(s) updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
