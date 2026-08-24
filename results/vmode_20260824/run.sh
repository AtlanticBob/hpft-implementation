#!/usr/bin/env bash
# V 的两条臂在真实流量上的对照实验。
#
# 问题：审计环的满刻度 V 应当是一个全局常数，还是随该 flow-set 的公平上限走
# （V_f = c*ehat_f）？理论说后者让标记变成尺度无关的量——"以自己的份额计，
# 超发了多长时间"——而前者让审计的反应快慢随邻居数量变化。离线核验给出 90 倍
# 的差别；这个实验问它在真实流量上是否可观察。
#
# 做法：把审计逼出来。稳态下执行面把流压在份额上，r<=e，账本不动、s 恒为 0，
# 两条臂无从区分。所以运行中把目的 VM 的额度一步压低——流仍按旧份额在发，
# 新份额更低，于是 r>e 直到跟踪律把它拉下来，账本在这段窗口里工作。
#
# 两个场景给出 ehat 的对比（同一个目的 VM，改竞争者数量）：
#   sparse   1 个 flow-set  -> ehat = VM 额度
#   crowded  3 个 flow-set  -> ehat = VM 额度 / 3
#
# usage: run.sh <arm: global|per_fs> <scene: sparse|crowded>
set -u
REPO=/home/zhaoxiang/hyperfront/hpft-implementation
DIR=$REPO/results/vmode_20260824
OUT=$DIR/results
ARM=$1; SCENE=$2; TAG="${ARM}_${SCENE}"
PT=$HOME/hyperfront/perftest-26015/ib_write_bw
RECV=sgpu02; RDPU=hpft-dpu2
CAP_HI=60000000000; CAP_LO=20000000000
SETTLE=20; AFTER=20
case "$SCENE" in
  sparse)  SENDERS="sgpu01" ;;
  crowded) SENDERS="sgpu01 sgpu03 sgpu04" ;;
  *) echo "unknown scene"; exit 1 ;;
esac
mkdir -p "$OUT"; cd "$REPO"

mkreg() {  # mkreg <cap> <outfile>
  python3 - "$1" "$2" "$ARM" <<'PY'
import json, sys
cap, out, arm = int(sys.argv[1]), sys.argv[2], sys.argv[3]
r = json.load(open("config/lab-registry.json"))
r["e_params"]["v_mode"] = arm
# 只压**接收端**的额度。连发送端一起压会让发送端的硬件限速器先把流砍掉，
# 接收端于是看不到持续超额，账本无事可做——刺激就落空了（sparse 场景实测
# s 只到 0.06）。要让审计工作，必须让发送端继续按旧速率发。
for vm in r["policy"]["vms"]:
    if vm.startswith("sgpu02/"):
        r["policy"]["vms"][vm]["max_rate_bps"] = cap
json.dump(r, open(out, "w"), indent=2)
PY
}
push() { for d in $(python3 -c "
import json;print(' '.join(n['dpu'] for n in json.load(open('config/lab-registry.json'))['nodes']))"); do
    scp -q "$1" "$d:/tmp/lr.json"; ssh -o BatchMode=yes "$d" 'sudo cp /tmp/lr.json /opt/hpft/lab-registry.json'
  done; }
restore() { for d in $(python3 -c "
import json;print(' '.join(n['dpu'] for n in json.load(open('config/lab-registry.json'))['nodes']))"); do
    scp -q config/lab-registry.json "$d:/opt/hpft/lab-registry.json" 2>/dev/null || true; done; }
trap restore EXIT

echo "== $TAG : arm=$ARM senders=$SENDERS =="
mkreg $CAP_HI /tmp/vm_hi.json; mkreg $CAP_LO /tmp/vm_lo.json
push /tmp/vm_hi.json
# 起 agent（v_mode 在启动时读，所以换臂必须重启）
bash tools/lab-infra/roles.sh set --receiver $RECV --senders "$(echo $SENDERS | tr ' ' ',')" >/dev/null
sleep 4

ssh -o BatchMode=yes $RECV 'pkill -f "ib_write_[b]"; true'; sleep 1
i=0; for s in $SENDERS; do
  ssh -o BatchMode=yes -f $RECV "setsid nohup $PT -d mlx5_6 -x 3 -p $((28100+i)) -q 4 --report_gbits -D $((SETTLE+AFTER+8)) >/tmp/vm_s$i 2>&1 </dev/null &"
  i=$((i+1)); done
sleep 2
i=0; for s in $SENDERS; do
  dev=$(python3 -c "
import json;r=json.load(open('config/lab-registry.json'))
print(next(v['rdma_dev'] for v in r['vnics'] if v['host']=='$s' and v['netdev']=='dpu1vf0'))")
  cmd="setsid nohup $PT -d $dev -x 3 -p $((28100+i)) -q 4 --report_gbits -D $((SETTLE+AFTER+8)) 10.1.0.2 >/tmp/vm_c$i 2>&1 </dev/null &"
  if [ "$s" = "$(hostname)" ]; then bash -c "$cmd"; else ssh -o BatchMode=yes "$s" "$cmd"; fi
  i=$((i+1)); done

t0=$(date +%s.%N); echo "$t0" > "$OUT/${TAG}_t0.txt"
sleep $SETTLE

# 前置门：阶跃之前必须确认场景真的成立——预期数量的 rdma 流集合同时在跑，
# 且它们的 ê 相等。不查这一条就阶跃，量到的会是流的起停而不是政策变更；
# crowded 首轮就是这么废掉的（ê 在 30/60G 之间跳、单流读数 138G）。
NWANT=$(echo $SENDERS | wc -w)
ok=$(ssh -o BatchMode=yes $RDPU "tail -40 /tmp/hpft_rxagent_e.jsonl" 2>/dev/null | python3 -c "
import json,sys
seen=[]
for line in sys.stdin:
    try: r=json.loads(line)
    except: continue
    c={k:v for k,v in (r.get('c') or {}).items() if k.endswith('rdma')}
    if c: seen.append(c)
if len(seen) < 10: print('few'); raise SystemExit
n={len(c) for c in seen[-10:]}
e={round(v/1e9,1) for c in seen[-10:] for v in c.values()}
print('ok' if n=={$NWANT} and len(e)==1 else 'unstable n=%s ehat=%s'%(n,e))")
if [ "$ok" != ok ]; then
  echo "  ABORT: 阶跃前场景不稳 ($ok) —— 不做无效的阶跃"
  exit 1
fi
echo "  前置门通过：$NWANT 个 rdma 流集合，ê 一致"

tstep=$(date +%s.%N); echo "$tstep" > "$OUT/${TAG}_tstep.txt"
push /tmp/vm_lo.json                 # 这一步就是实验刺激：额度一步压低
echo "  cap stepped $((CAP_HI/1000000000))G -> $((CAP_LO/1000000000))G at t=$SETTLE s"
sleep $AFTER

scp -q "$RDPU:/tmp/hpft_rxagent_e.jsonl" "$OUT/${TAG}_rx.jsonl"
ssh -o BatchMode=yes $RECV 'pkill -f "ib_write_[b]"; true'
echo "  -> $OUT/${TAG}_rx.jsonl"
