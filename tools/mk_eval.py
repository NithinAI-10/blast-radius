"""Make an eval dir (commits/ + tests.json + answers.json) from patches and label files.

Usage: python mk_eval.py PATCH_DIR OUT_DIR LABEL_JSON...
Commits whose labels have unknown tests are skipped.
"""
import json, os, shutil, sys

pdir, out = sys.argv[1], sys.argv[2]
labels = {}
for f in sys.argv[3:]:
    labels.update(json.load(open(f)))
os.makedirs(out + "/commits", exist_ok=True)
shutil.copy("/home/claude/kit/sample/tests.json", out + "/tests.json")
ans = {}
for cid, v in labels.items():
    if v["unknown"]:
        continue
    src = os.path.join(pdir, cid + ".patch")
    if not os.path.exists(src):
        continue
    shutil.copy(src, f"{out}/commits/{cid}.patch")
    ans[cid] = {"failing": v["failing"], "kind": "gen"}
json.dump({"commits": ans}, open(out + "/answers.json", "w"))
print(len(ans), "commits")
