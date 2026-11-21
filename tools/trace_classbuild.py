"""For sqlglot function-body lines that run while classes are being built at import (metaclass __new__,
__init_subclass__, helpers called from class bodies), the classes under construction at that moment.
usage: trace_classbuild.py sqlglot_repo out.json"""
import importlib, json, os, pkgutil, sys
repo, out = sys.argv[1:3]
import unittest  # noqa: F401  (UNITTEST on, as under pytest)
sys.path.insert(0, repo)
root = os.path.join(repo, "sqlglot") + os.sep
hits = {}; seen = {}
M = sys.monitoring; T = 5
M.use_tool_id(T, "classbuild")
CAP = 3000

def building(frame):
    f = frame
    while f is not None:
        loc = f.f_locals if f.f_code.co_name != "<module>" else {}
        name = f.f_code.co_name
        if name == "__new__" and "attrs" in loc and isinstance(loc.get("attrs"), dict) and "__qualname__" in loc["attrs"]:
            a = loc["attrs"]; return a.get("__module__", "?") + "." + a["__qualname__"]
        if name == "__init_subclass__" and isinstance(loc.get("cls"), type):
            c = loc["cls"]; return c.__module__ + "." + c.__qualname__
        if "__qualname__" in loc and "__module__" in loc and isinstance(loc.get("__qualname__"), str) \
                and name == loc["__qualname__"].rsplit(".", 1)[-1]:
            return loc["__module__"] + "." + loc["__qualname__"]  # a class body
        f = f.f_back
    return None

def on_line(code, line):
    fn = code.co_filename
    if not fn.startswith(root):
        return M.DISABLE
    if code.co_name == "<module>" or (code.co_flags & 0x02) == 0:  # module or class body: not a function
        return M.DISABLE
    key = (os.path.relpath(fn, repo), line)
    n = seen.get(key, 0) + 1; seen[key] = n
    if n > CAP:
        return M.DISABLE
    k = building(sys._getframe(1))
    if k:
        hits.setdefault(key, set()).add(k)
    return None

M.register_callback(T, M.events.LINE, on_line)
M.set_events(T, M.events.LINE)
import sqlglot
for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
    if info.name.endswith("__main__"): continue
    try: importlib.import_module(info.name)
    except Exception as e: print("skip", info.name, e, file=sys.stderr)
M.set_events(T, 0)
json.dump([[f, l, sorted(v)] for (f, l), v in sorted(hits.items())], open(out, "w"))
print(len(hits), "lines;", "capped:", sum(1 for v in seen.values() if v > CAP), file=sys.stderr)
