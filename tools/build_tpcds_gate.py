"""tpcds_gate.json.gz: lines test_execute_tpcds runs outside optimize (from trace_tpcds_phases.py), the
TPC-DS queries it executes and their schema. usage: build_tpcds_gate.py sqlglot_repo phases.json out"""
import gzip, json, os, sys
repo, phases, out = sys.argv[1:4]
sys.path.insert(0, repo)
from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs
os.chdir(repo)
qs = [sql for meta, sql, _ in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql") if meta.get("execute")]
ph = json.load(open(phases))
outside = {}
for f, l in ph["outside"]: outside.setdefault(f, []).append(l)
open(out, "wb").write(gzip.compress(json.dumps({"outside": outside, "queries": qs, "schema": TPCDS_SCHEMA}).encode(), 9, mtime=0))
print(len(qs), "queries", sum(map(len, outside.values())), "outside lines")
