#!/usr/bin/env python3
"""Render a .flows file as the markdown flow table used in README.md, with the
VF IPs filled in from the registry. usage: render_table.py <file.flows>"""
import json, sys, os
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
R = json.load(open(os.path.join(REPO, 'config', 'lab-registry.json')))
ip = {(v['host'], v['netdev']): v['ip'] for v in R['vnics']}
print('| 行 | 源 VM | 目的 VM | 类型 | 数量 | 起 (s) | 止 (s) | 备注 |')
print('|---|---|---|---|---|---|---|---|')
k = 0
for line in open(sys.argv[1]):
    if not line.strip() or line.startswith('#'):
        continue
    h, sv, dv, cls, n, a, b, opt = line.split()
    k += 1
    if cls == 'meter':          # not a flow: the hidden bottleneck of V8
        g = opt.split('=')[-1]
        print(f'| {k} | —（接收端自己） | sgpu02/vf{dv} ({ip[("sgpu02","dpu1vf"+dv)]}) | '
              f'隐形瓶颈：OVS drop meter | — | {a} | {b} | '
              f'meter 压到 {g} G，注册表仍写 50 G——账本看不见这个瓶颈，只看得见活下来的到达量 |')
        continue
    ty = {'rdma': 'RDMA WRITE', 'tcp': 'TCP iperf3', 'udp': 'UDP 背景（udp_blast）'}[cls]
    cnt = f'{n} QP' if cls == 'rdma' else (f'{n} 流' if cls == 'tcp' else '—')
    note = {'-': '', 'rate_limit=10': '`--rate_limit 10`（应用需求 10 G）', 'tcp_cc=bbr': 'iperf3 `-C bbr`', 'rdma_cc=swift': '执行面 Swift 项（`0xccd 3`）', 'gbps=40': '40 G，HyperFront 不调度也不整形的流量'}.get(opt, opt)
    print(f'| {k} | {h}/vf{sv} ({ip[(h,"dpu1vf"+sv)]}) | sgpu02/vf{dv} ({ip[("sgpu02","dpu1vf"+dv)]}) | {ty} | {cnt} | {a} | {b} | {note} |')
