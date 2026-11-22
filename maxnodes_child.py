"""A scaled-down replica of tests/test_parser.py::TestParser::test_max_nodes (28.8 s: it parses a
10000-column query). Every step of the test is replayed with the same structure; the giant query is
cut to a fixed fraction with the node limit cut by the same fraction, so the limit is hit the same way.
The fingerprint (result trees, exception classes and messages) is compared with the pinned sqlglot's.
Used by the image build (baseline, `python -S maxnodes_child.py <sg_root> <out.json>`) and by the
selector (forked child, patched sources swapped in): same code both times."""
import hashlib


def _canon(e, exp):
    if isinstance(e, exp.Expr):
        return (type(e).__name__, tuple((k, _canon(v, exp)) for k, v in sorted(e.args.items())))
    if isinstance(e, list):
        return tuple(_canon(x, exp) for x in e)
    return repr(e)


def _run(fn):
    from sqlglot import exp
    try:
        r = fn()
        return ["ok", type(r).__name__, hashlib.sha1(repr(_canon(r, exp)).encode()).hexdigest()[:16]]
    except Exception as e:  # noqa: BLE001
        return ["err", type(e).__module__ + "." + type(e).__name__, str(e)[:400]]


def fingerprint():
    from sqlglot import parse_one
    out = []
    out.append(_run(lambda: parse_one("SELECT 1", max_nodes=1_000_000)))
    cols = [f"col{i}" for i in range(500)]
    sql = f"SELECT {','.join(cols)} FROM t"
    out.append(_run(lambda: parse_one(sql, max_nodes=1_000_000)))
    depth = 100
    expr = "x" + "=x" * depth
    for width, limit in ((100, 10_000), (200, 20_000), (600, 50_000), (40, 1_000_000)):
        big = f"SELECT {','.join(expr for _ in range(width))}"
        out.append(_run(lambda: parse_one(big, max_nodes=limit)))
    out.append(_run(lambda: parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)))
    out.append(_run(lambda: parse_one("SELECT 1, 2, 3")))
    return out


def check(base):
    return fingerprint() == base


if __name__ == "__main__":
    import json, sys
    root, dst = sys.argv[1:3]
    import unittest  # noqa: F401  sqlglot's UNITTEST flag is on under the test runner
    sys.path.insert(0, root)
    json.dump(fingerprint(), open(dst, "w"))
