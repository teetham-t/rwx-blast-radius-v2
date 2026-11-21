"""Which sqlglot lines run while each test module is imported (pytest collection), per module, in a
fresh interpreter. Coverage with per-test contexts attributes none of these lines to a test.
usage: trace_collect.py sqlglot_repo out.json"""
import json, os, subprocess, sys
repo, out = sys.argv[1:3]
CHILD = r'''
import sys, os, json
repo = sys.argv[1]; mod = sys.argv[2]
import unittest  # sqlglot's UNITTEST flag is on under pytest
sys.path.insert(0, repo)
pre = sys.argv[3] == "1"
if pre:
    import sqlglot  # the package import itself is traced separately
root = os.path.join(repo, "sqlglot") + os.sep
hits = set()
M = sys.monitoring; TOOL = 3
M.use_tool_id(TOOL, "collect")
def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(root):
        hits.add((os.path.relpath(fn, repo), line))
        return None
    return M.DISABLE
M.register_callback(TOOL, M.events.LINE, on_line)
M.set_events(TOOL, M.events.LINE)
import importlib
try:
    importlib.import_module(mod)
except Exception as e:
    print("ERR", mod, e, file=sys.stderr)
M.set_events(TOOL, 0)
json.dump(sorted(hits), sys.stdout)
'''
mods = []
for rootd, _, fs in os.walk(os.path.join(repo, "tests")):
    if "fixtures" in rootd: continue
    for fn in fs:
        if fn.startswith("test_") and fn.endswith(".py"):
            rel = os.path.relpath(os.path.join(rootd, fn), repo)
            mods.append(rel)
res = {}
env = dict(os.environ, SKIP_INTEGRATION="0")
r = subprocess.run([sys.executable, "-c", CHILD, repo, "sqlglot", "0"], capture_output=True, text=True, cwd=repo, env=env)
res["*"] = json.loads(r.stdout)
for rel in sorted(mods):
    m = rel[:-3].replace("/", ".")
    r = subprocess.run([sys.executable, "-c", CHILD, repo, m, "1"], capture_output=True, text=True, cwd=repo, env=env)
    try: res[rel] = json.loads(r.stdout)
    except Exception: print("fail", rel, r.stderr[-300:]); continue
    print(rel, len(res[rel]), file=sys.stderr)
json.dump(res, open(out, "w"))
