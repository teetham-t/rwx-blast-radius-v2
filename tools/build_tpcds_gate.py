"""tpcds_gate.json.gz: lines test_execute_tpcds runs outside optimize (from trace_tpcds_phases.py), the
TPC-DS queries it executes and their schema. Also, per sqlglot line, which of those queries' optimize() runs it (tools/trace_tpcds_queries.py, cold).
usage: build_tpcds_gate.py sqlglot_repo phases.json out [queries.json]"""
import gzip, json, os, sys
repo, phases, out = sys.argv[1], sys.argv[2], os.path.abspath(sys.argv[3])
sys.path.insert(0, repo)
from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs
os.chdir(repo)
qs = [sql for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql") if meta.get("execute")]
ph = json.load(open(phases))
outside = {}
for f, l in ph["outside"]: outside.setdefault(f, []).append(l)
inside = {}
for f, l in ph["inside_only"]: inside.setdefault(f, []).append(l)
per = {}
if len(sys.argv) > 4:
    for i, lines in enumerate(json.load(open(sys.argv[4]))):
        for f, l in lines:
            per.setdefault(f, {}).setdefault(str(l), 0)
            per[f][str(l)] |= 1 << i
out = os.path.abspath(out) if not os.path.isabs(out) else out
open(out, "wb").write(gzip.compress(json.dumps({"outside": outside, "inside": inside, "queries": qs, "schema": TPCDS_SCHEMA, "per_query": per}).encode(), 9, mtime=0))
print(len(qs), "queries", sum(map(len, outside.values())), "outside lines")
