"""Per TPC-DS query (the ones test_execute_tpcds executes), the sqlglot lines optimize() runs, each query
in a fresh interpreter (so work cached by the first query is credited to every query that needs it).
usage: trace_tpcds_queries.py sqlglot_repo gate.json.gz out.json"""
import gzip, json, os, subprocess, sys
repo, gate, out = sys.argv[1:4]
CHILD = r'''
import sys, os, json, gzip
repo, gate, i = sys.argv[1], sys.argv[2], int(sys.argv[3])
import unittest  # sqlglot's UNITTEST flag is on under the test runner
sys.path.insert(0, repo)
g = json.loads(gzip.decompress(open(gate, "rb").read()))
from sqlglot import parse_one
from sqlglot.optimizer import optimize
root = os.path.join(repo, "sqlglot") + os.sep
hits = set(); M = sys.monitoring; T = 3
M.use_tool_id(T, "q")
def on_line(code, line):
    if code.co_filename.startswith(root):
        hits.add((os.path.relpath(code.co_filename, repo), line)); return M.DISABLE
    return M.DISABLE
M.register_callback(T, M.events.LINE, on_line)
import pickle
e = pickle.loads(pickle.dumps(parse_one(g["queries"][i])))  # as the test's process pool does
M.set_events(T, M.events.LINE)
optimize(e, g["schema"], leave_tables_isolated=True, dialect=None)
M.set_events(T, 0)
json.dump(sorted(hits), sys.stdout)
'''
g = json.loads(gzip.decompress(open(gate, "rb").read()))
res = []
for i in range(len(g["queries"])):
    r = subprocess.run([sys.executable, "-S", "-c", CHILD, repo, gate, str(i)], capture_output=True, text=True)
    res.append(json.loads(r.stdout))
json.dump(res, open(out, "w"))
print(len(res), "queries; lines per query:", sorted(len(x) for x in res)[::8])
