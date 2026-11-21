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


def hashes(queries, schema, stop_at=None):
    """yield the hash of each optimized query; stop_at: list of expected hashes (stop at first mismatch)"""
    from sqlglot import exp, parse_one
    from sqlglot.optimizer import optimize
    out = []
    for i, sql in enumerate(queries):
        o = optimize(parse_one(sql), schema, leave_tables_isolated=True, dialect=None)
        h = hashlib.sha1(repr(canon(o, exp)).encode()).hexdigest()[:16]
        out.append(h)
        if stop_at is not None and stop_at[i] != h:
            return out, False
    return out, True


if __name__ == "__main__":
    import gzip, json, sys
    root, gate, dst = sys.argv[1:4]
    sys.path.insert(0, root)
    g = json.loads(gzip.decompress(open(gate, "rb").read()))
    hs, _ = hashes(g["queries"], g["schema"])
    json.dump(hs, open(dst, "w"))
