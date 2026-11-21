"""Lines test_execute_tpcds runs outside optimize() (setup, parsing, planning, execution, the duckdb side)
vs only inside it. usage: trace_tpcds_phases.py sqlglot_repo out.json"""
import json, os, sys, unittest
repo, out = sys.argv[1:3]
os.chdir(repo); sys.path.insert(0, repo); sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["SKIP_INTEGRATION"] = "0"
import inline_pool  # noqa: F401  (pool work inline, so it is traced)
import tests.test_executor as TE
import sqlglot.executor as EX
root = os.path.join(repo, "sqlglot") + os.sep
inside = set(); outside = set(); state = {"in": 0, "stage": "setup"}
stages = {}
M = sys.monitoring; TOOL = 4
M.use_tool_id(TOOL, "phases")
def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(root):
        key = (os.path.relpath(fn, repo), line)
        st = "optimize" if state["in"] else state["stage"]
        s_ = stages.setdefault(key, set())
        if st not in s_:
            s_.add(st)
        if state["in"]:
            inside.add(key)
            return None
        outside.add(key)
        return M.DISABLE if st in ("exec", "setup") else None  # exec/setup block the gate anyway
    return M.DISABLE
M.register_callback(TOOL, M.events.LINE, on_line)
orig = EX.optimize
def wrapped(*a, **k):
    state["in"] += 1
    try: return orig(*a, **k)
    finally: state["in"] -= 1
EX.optimize = wrapped
import sqlglot
def staged(name, fn):
    def w(*a, **k):
        prev = state["stage"]; state["stage"] = name
        try: return fn(*a, **k)
        finally: state["stage"] = prev
    return w
TE.parse_one = staged("parse", TE.parse_one)            # fixture SQL parsed in the test
TE.transpile = staged("duck", TE.transpile)             # the duckdb side of the comparison
TE.execute = staged("exec", TE.execute)                 # execute(): optimize (tagged above), plan, run
TE.find_tables = staged("exec", TE.find_tables)         # picks the tables execute() gets
M.set_events(TOOL, M.events.LINE)
TE.TestExecutor.setUpClass()
state["stage"] = "test"
t = TE.TestExecutor("test_execute_tpcds"); r = unittest.TestResult(); t.run(r)
M.set_events(TOOL, 0)
print("ok" if r.wasSuccessful() else ("FAIL", r.failures[:1], r.errors[:1]), file=sys.stderr)
json.dump({"outside": sorted(outside), "inside_only": sorted(inside - outside),
           "stages": [[f, l, sorted(v)] for (f, l), v in sorted(stages.items())]}, open(out, "w"))
print(len(outside), "outside;", len(inside - outside), "inside only", file=sys.stderr)
