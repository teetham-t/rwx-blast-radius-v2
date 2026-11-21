"""Canonical hashes of optimize() for the TPC-DS queries test_execute_tpcds executes. Used by the image
build (baseline, `python -S tpcds_gate_child.py <sg_root> <gate.json.gz> <out.json>`) and by the selector
(forked child, patched sources swapped in): same code both times."""
import hashlib


def canon(e, exp):
    if isinstance(e, exp.Expr):
        t = e.type.sql() if getattr(e, "_type", None) is not None else None
        return (type(e).__name__, tuple((k, canon(v, exp)) for k, v in sorted(e.args.items())), t)
    if isinstance(e, list):
        return tuple(canon(x, exp) for x in e)
    return repr(e)


def hashes(queries, schema, stop_at=None, only=None):
    """hash of each optimized query; stop_at: expected hashes (stop at first mismatch); only: query indices"""
    from sqlglot import exp, parse_one
    from sqlglot.optimizer import optimize
    out = []
    for i, sql in enumerate(queries):
        if only is not None and i not in only:
            out.append(None); continue
        o = optimize(parse_one(sql), schema, leave_tables_isolated=True, dialect=None)
        h = hashlib.sha1(repr(canon(o, exp)).encode()).hexdigest()[:16]
        out.append(h)
        if stop_at is not None and stop_at[i] != h:
            return out, False
    return out, True


def _h(x):
    return hashlib.sha1(x.encode()).hexdigest()[:16]


def parse_hashes(queries, stop_at=None):
    """the test parses each fixture query with parse_one before executing it"""
    from sqlglot import exp, parse_one
    out = []
    for i, sql in enumerate(queries):
        out.append(_h(repr(canon(parse_one(sql), exp))))
        if stop_at is not None and stop_at[i] != out[-1]:
            return out, False
    return out, True


def duck_hashes(queries, stop_at=None):
    """the expected side: transpile(sql, write="duckdb")[0], run by duckdb"""
    from sqlglot import transpile
    out = []
    for i, sql in enumerate(queries):
        out.append(_h(transpile(sql, write="duckdb")[0]))
        if stop_at is not None and stop_at[i] != out[-1]:
            return out, False
    return out, True


def check(g, stages, only):
    """stages: subset of "pod"; only: optimize query indices (None = all). True when all identical"""
    b = g["base"]
    if "p" in stages and not parse_hashes(g["all_queries"], b["parse"])[1]:
        return False
    if "d" in stages and not duck_hashes(g["queries"], b["duck"])[1]:
        return False
    if "o" in stages and not hashes(g["queries"], g["schema"], stop_at=b["opt"], only=only)[1]:
        return False
    return True


if __name__ == "__main__":
    import gzip, json, sys
    root, gate, dst = sys.argv[1:4]
    sys.path.insert(0, root)
    g = json.loads(gzip.decompress(open(gate, "rb").read()))
    json.dump({"opt": hashes(g["queries"], g["schema"])[0], "parse": parse_hashes(g["all_queries"])[0],
               "duck": duck_hashes(g["queries"])[0]}, open(dst, "w"))
