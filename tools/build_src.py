"""Bundle the pinned sqlglot + test sources (so the selector can rebuild patched files) and the line
spans of every def/class and of every module- or class-level statement. usage: build_src.py repo out"""
import ast, gzip, os, pickle, subprocess, sys
repo, out = sys.argv[1:3]
fs = [f for f in subprocess.check_output(["git", "ls-files", "sqlglot", "tests"], cwd=repo, text=True).split()
      if f.endswith(".py") and not f.startswith("tests/fixtures/")]
src = {}; spans = {}; sigs = {}
for f in fs:
    s = open(os.path.join(repo, f), encoding="utf-8").read(); src[f] = s
    tree = ast.parse(s); sp = set()
    def first(n): return min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            sp.add((first(n), n.end_lineno))
        if isinstance(n, (ast.Module, ast.ClassDef)):
            for c in n.body: sp.add((first(c), c.end_lineno))
    spans[f] = sorted(sp, key=lambda x: (x[1] - x[0], x[0]))  # smallest first
    # signatures: decorators + def line(s) up to the line before the body (defaults run at def time)
    sigs[f] = sorted((first(n), n.body[0].lineno - 1, n.end_lineno) for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.body[0].lineno > n.lineno)
open(out, "wb").write(gzip.compress(pickle.dumps({"src": src, "spans": spans, "sigs": sigs}, protocol=4), 9, mtime=0))
print(len(src), "files")
