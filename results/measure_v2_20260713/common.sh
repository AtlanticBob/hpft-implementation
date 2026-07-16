# Shared recorder plumbing for the measurement-v2 campaign (2026-07-13).
# Sourced by run_*.sh. Three parallel records per run, production untouched:
#   old attribution : production rx_agent's own jsonl (sliced by byte offset)
#   new attribution : bypass rx_agent (meter-only, --vport-meter, port 9713)
#   ground truth    : gt_sampler4.py on sgpu02 (port_rcv_data + netdev rx)
DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

start_recorders() {   # <tag> <secs>
  local tag=$1 secs=$2
  PROD_OFF=$(ssh hpft-dpu2 'stat -c%s /tmp/hpft_rxagent_e.jsonl 2>/dev/null || echo 0')
  ssh -f hpft-dpu2 "sudo timeout $((secs+25)) /tmp/vport_meter mlx5_1 /dev/shm/hpft_vpm 1,2,3,4 1 > /tmp/${tag}_vpm.log 2>&1"
  ssh -f hpft-dpu2 "sudo rm -f /tmp/${tag}_bypass.jsonl; cd /tmp/hpft_bypass && sudo timeout $((secs+20)) python3 rx_agent.py --registry /tmp/hpft_bypass/lr_bypass.json --vport-meter /dev/shm/hpft_vpm --meter-only --log /tmp/${tag}_bypass.jsonl --duration $((secs+10)) > /tmp/${tag}_bypass.out 2>&1"
  ssh -f sgpu02 "python3 /tmp/gt_sampler4.py 0.02 $secs > /tmp/${tag}_gt.tsv 2>/dev/null"
  sleep 2
}

collect() {           # <tag>
  local tag=$1
  ssh hpft-dpu2 "sudo tail -c +$((PROD_OFF+1)) /tmp/hpft_rxagent_e.jsonl > /tmp/${tag}_prod.jsonl"
  scp -q "hpft-dpu2:/tmp/${tag}_bypass.jsonl" "hpft-dpu2:/tmp/${tag}_prod.jsonl" \
         "hpft-dpu2:/tmp/${tag}_bypass.out" "sgpu02:/tmp/${tag}_gt.tsv" "$DIR/"
}
