#!/usr/bin/env bash
# quick_sw.sh <name> <spec> [VAR=value ...]
# quick.sh plus the switch's view of the receiver port: ECN-marked frames and
# queue drops on swp37s0 (sgpu02) over the run, read through NVUE on sn5600.
# The switch counter is the only trustworthy measure of marking: the
# executor's CNP counter saturates at the receiver NIC's CNP generation rate
# (~2 M per sender per 12 s here) and cannot tell 20 M marks from 180 M.
REPO=$(cd "$(dirname "$0")/../.." && pwd); cd "$REPO"
NAME=$1; SPEC=$2; shift 2
PORT=${SW_PORT:-swp37s0}
snap() { ssh -o BatchMode=yes sn5600 "nv show interface $PORT counters 2>/dev/null" 2>/dev/null | python3 -c "
import sys,re
t=sys.stdin.read()
m=re.search(r'ECN Marked Packets\s+n/a\s+(\d+)',t); e=int(m.group(1)) if m else -1
m=re.search(r'Queue Drops\s+n/a\s+(\d+)',t); d=int(m.group(1)) if m else -1
i=t.find('traffic-class  tx-frames'); m=re.search(r'\n\s+0\s+(\d+)\s+([\d.]+) ([KMGT]?B)\s',t[i:]) if i>=0 else None
mult={'B':1,'KB':1e3,'MB':1e6,'GB':1e9,'TB':1e12}
fr=int(m.group(1)) if m else -1; by=int(float(m.group(2))*mult[m.group(3)]) if m else -1
print('ecn=%d drops=%d frames=%d bytes=%d'%(e,d,fr,by))"; }
A=$(snap); env "$@" bash validation/run/quick.sh "$NAME" 12 validation/scenarios/$SPEC.spec >/dev/null 2>&1; B=$(snap)
python3 - "$A" "$B" "$NAME" "$*" "$PORT" <<'PY'
import sys
a=dict(kv.split('=') for kv in sys.argv[1].split()); b=dict(kv.split('=') for kv in sys.argv[2].split())
d={k:int(b[k])-int(a[k]) for k in a}
tot=[l for l in open('/tmp/quick_%s/result.txt'%sys.argv[3]) if l.startswith('TOTAL')][0].strip()
wire=d['bytes']*8/12.0/1e9 if d['bytes']>0 else 0.0
line='== %s (%s): %s | switch %s: ecn_marked=%d drops=%d wire=%.1fG (%.0f%% of 200)'%(sys.argv[3],sys.argv[4],tot,sys.argv[5],d['ecn'],d['drops'],wire,wire/2.0)
print(line); open('/tmp/quick_%s/switch.txt'%sys.argv[3],'w').write(line+'\n')
PY
grep -v "^#\|TOTAL" /tmp/quick_$NAME/result.txt | awk '{printf "   %s %s\n",$3,$4}' | paste -sd' ' | fold -w 150
