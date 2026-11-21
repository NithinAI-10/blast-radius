#!/bin/sh
# Usage: tools/regress.sh  -> runs selector on sample, mut (120) and mut2 (labeled so far); prints totals
. /home/claude/venv/bin/activate
cd /home/claude
rm -rf evalm evaln
python tools/mk_eval.py mut/commits evalm labels/mut_0.json labels/mut_1.json >/dev/null
python tools/mk_eval.py mut2/commits evaln $(ls labels/mut2_*.json) >/dev/null
for d in kit/sample evalm evaln; do
  n=$(basename $d)
  BR_DATA=$d BR_BASE=/home/claude/sqlglot BR_TMP=/tmp/brtmp python tools/cpurun.py python entry/selector.py > /tmp/o_$n.jsonl 2> /tmp/e_$n.txt
  cpu=$(grep -o 'CPU=[0-9.]*' /tmp/e_$n.txt | tail -1 | cut -d= -f2)
  sim=$(python kit/score.py $d $d/answers.json /tmp/o_$n.jsonl 2>/dev/null | grep Simulated | awk '{print $2}')
  miss=$(python kit/score.py $d $d/answers.json /tmp/o_$n.jsonl 2>/dev/null | grep Misses | awk '{print $2}')
  cnt=$(ls $d/commits | wc -l)
  python3 -c "print(f'$n: commits=$cnt sim=$sim cpu=$cpu misses=$miss total={float(\"$sim\")+float(\"$cpu\"):.1f} per_commit={(float(\"$sim\")+float(\"$cpu\"))/$cnt:.2f}')"
done
