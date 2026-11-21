"""Runs at image build (not charged): decompress the maps and precompute the tables select_tests
would otherwise derive at every start. Writes index.pkl and src.pkl next to this file."""
import gzip, os, pickle
HERE = os.path.dirname(os.path.abspath(__file__))
I = pickle.loads(gzip.decompress(open(os.path.join(HERE, "index.pkl.gz"), "rb").read()))
mc = {}
for f, sp in I["spans"].items():
    lm = I["linemap"].get(f, {})
    for a, b, n in sp:
        m = 0
        for l in range(a, b + 1): m |= lm.get(l, 0)
        mc[n] = mc.get(n, 0) | m
kba = {}
for (c, a, k), m in I["keys"].items():
    kba.setdefault((c, a), []).append((k, m))
I["_method_cov"] = mc; I["_keys_by_attr"] = kba
open(os.path.join(HERE, "index.pkl"), "wb").write(pickle.dumps(I, protocol=5))
_S = pickle.loads(gzip.decompress(open(os.path.join(HERE, "src.pkl.gz"), "rb").read()))
# direct importers of every sqlglot module (static, any scope), for "this module fails to import"
import ast
def _modname(f):
    m = f[:-3].replace("/", ".")
    return m[:-9] if m.endswith(".__init__") else m
by_mod = {_modname(f): f for f in _S["src"] if f.startswith("sqlglot/")}
importers = {}
for g, text in _S["src"].items():
    if not g.startswith("sqlglot/"): continue
    try: tree = ast.parse(text)
    except SyntaxError: continue
    pkg = _modname(g) if g.endswith("__init__.py") else _modname(g).rpartition(".")[0]
    for n in ast.walk(tree):
        targets = []
        if isinstance(n, ast.Import):
            targets = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = n.module or ""
            if n.level:
                parts = pkg.split(".")
                base = ".".join(parts[:len(parts) - (n.level - 1)] + ([n.module] if n.module else []))
            targets = [base] + [base + "." + a.name for a in n.names]
        for t in targets:
            while t:
                if t in by_mod and by_mod[t] != g:
                    importers.setdefault(by_mod[t], set()).add(g)
                t = t.rpartition(".")[0] if t not in by_mod else ""
_S["importers"] = importers
open(os.path.join(HERE, "src.pkl"), "wb").write(pickle.dumps(_S, protocol=5))

# word indexes for the text searches (loaded only when a patch needs them)
import re
ident_cov = {}
for f, d in I["linemap"].items():
    src = I["covsrc"].get(f, {})
    for ln, m in d.items():
        for w in set(re.findall(r"\w+", src.get(ln, ""))):
            ident_cov[w] = ident_cov.get(w, 0) | m
test_words = {}
for i, s in I["test_src"].items():
    for w in set(t.lower() for t in re.findall(r"[A-Za-z0-9_]+", s)):
        test_words[w] = test_words.get(w, 0) | (1 << i)
open(os.path.join(HERE, "words.pkl"), "wb").write(pickle.dumps({"ident_cov": ident_cov, "test_words": test_words}, protocol=5))

# the pinned sqlglot package as a tree, for import checks (compiled to .pyc by the Dockerfile)
_s = pickle.loads(open(os.path.join(HERE, "src.pkl"), "rb").read())["src"]
for f, text in _s.items():
    if not f.startswith("sqlglot/"):
        continue
    dst = os.path.join(HERE, "sg", f)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as fh:
        fh.write(text)

# baseline for the test_execute_tpcds gate, computed with the pinned tree (same code as the runtime check)
import subprocess, sys
subprocess.run([sys.executable, "-S", os.path.join(HERE, "tpcds_gate_child.py"), os.path.join(HERE, "sg"),
                os.path.join(HERE, "tpcds_gate.json.gz"), os.path.join(HERE, "tpcds_base.json")], check=True,
               stderr=subprocess.DEVNULL)
