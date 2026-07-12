#!/usr/bin/env bash
# Class-attribution robustness under sustained 4-pair mixed load, testing
# the user's hint: strong iperf3 bind (-B ip%dev, SO_BINDTODEVICE) vs plain
# -B ip. Two 50s phases; sample rx nfs + per-VF tcp every 10s. If plain
# binding lets TCP leak off the intended VF (wrong netdev counter on the
# receiver -> misattribution -> TCP fail-open), the bound phase should hold
# nfs=8 and per-VF tcp>0 where the unbound phase degrades.
set -u
DIR=$(cd "$(dirname "$0")" && pwd)
PT=$HOME/hyperfront/perftest-26015/ib_write_bw

sample() { # label
    for k in 1 2 3 4; do
        sleep 10
        line=$(ssh hpft-dpu2 'tail -1 /tmp/hpft_rxagent_e.jsonl')
        echo "$line" | python3 -c "
import json,sys
r=json.load(sys.stdin)
tc={k.split('/')[1].split('>')[0]:v/1e9 for k,v in r['r'].items() if k.endswith('tcp')}
print('  $label t=%ds nfs=%d ha=%d  tcp: %s' % (${k}*10, r['nfs'], r['ha'],
      ' '.join('%s=%.1f'%(k,tc.get('vf%d'%i,0)) for i,k in enumerate(['vf0','vf1','vf2','vf3']))))
"
    done
}

phase() { # label bindsuffix
    local label=$1 sfx=$2
    ssh sgpu02 'pkill -f "ib_write_[b]"; true'; sleep 1
    for n in 0 1 2 3; do ssh -f sgpu02 "$PT -d mlx5_$((6+n)) -p $((24080+n)) --report_gbits -D 55 > /tmp/bd_s$n.log 2>&1"; done
    sleep 1
    for n in 0 1 2 3; do
        $PT -d mlx5_$((6+n)) -p $((24080+n)) --report_gbits -D 55 "10.1.$n.2" >/dev/null 2>&1 &
        iperf3 -B "10.1.$n.1${sfx//N/$n}" -c "10.1.$n.2" -p $((5201+4*n)) -P4 -b 0 -t 55 >/dev/null 2>&1 &
    done
    sleep 3
    sample "$label"
    wait 2>/dev/null
}

echo "=== PHASE A: plain bind (-B 10.1.N.1) ==="
phase A ""
echo "=== PHASE B: strong bind (-B 10.1.N.1%dpu1vfN) ==="
phase B "%dpu1vfN"
ssh sgpu02 'pkill -f "ib_write_[b]"; true'
echo bind-diag-done
