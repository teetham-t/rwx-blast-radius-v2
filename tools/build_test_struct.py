"""Static structure of the test package at the pinned commit: which test modules import which,
class spans and bases, test-method spans. usage: build_test_struct.py sqlglot_repo out.json"""
import ast, json, os, sys
repo, out = sys.argv[1:3]
res = {}
for root, _, fs in os.walk(os.path.join(repo, "tests")):
    if "fixtures" in root: continue
    for fn in fs:
        if not fn.endswith(".py"): continue
        p = os.path.join(root, fn); rel = os.path.relpath(p, repo)
        tree = ast.parse(open(p, encoding="utf-8").read())
        imps = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("tests"):
                imps.add(n.module.replace(".", "/") + ".py")
                for a in n.names: imps.add(n.module.replace(".", "/") + "/" + a.name + ".py")
            elif isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.startswith("tests"): imps.add(a.name.replace(".", "/") + ".py")
        classes = []
        for n in tree.body:
            if isinstance(n, ast.ClassDef):
                bases = [b.id if isinstance(b, ast.Name) else getattr(b, "attr", "") for b in n.bases]
                meths = [(m.name, (m.decorator_list[0].lineno if m.decorator_list else m.lineno), m.end_lineno)
                         for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
                classes.append({"name": n.name, "a": (n.decorator_list[0].lineno if n.decorator_list else n.lineno),
                                "b": n.end_lineno, "bases": bases, "methods": meths})
        used = lambda n: sorted({x.id for x in ast.walk(n) if isinstance(x, ast.Name)})
        defs = [(n.name, (n.decorator_list[0].lineno if n.decorator_list else n.lineno), n.end_lineno, used(n))
                for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        defs += [(t.id, n.lineno, n.end_lineno, used(n)) for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                 for t in (n.targets if isinstance(n, ast.Assign) else [n.target]) if isinstance(t, ast.Name)]
        # module-level names a class body or method reads (class bodies count as users too)
        defs += [(n.name, n.lineno, n.end_lineno, used(n)) for n in tree.body if isinstance(n, ast.ClassDef)]
        names = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} |
                       {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})
        res[rel] = {"imports": sorted(imps), "classes": classes, "defs": defs, "names": names}
for f, d in res.items():
    d["imports"] = [i for i in d["imports"] if i in res or i.endswith("__init__.py")]
json.dump(res, open(out, "w"), separators=(",", ":"))
print(len(res), "test modules")
