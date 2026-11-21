"""collect_map.pkl.gz from trace_collect.py output: lines run by `import sqlglot` ("core") and, per test
module, the lines its collection runs (grouped) and the sqlglot files involved.
usage: build_collect_map.py collect_trace.json out"""
import gzip, json, pickle, sys
C = json.load(open(sys.argv[1]))
core = {}
for f, l in C.pop("*"): core.setdefault(f, set()).add(l)
tfiles = sorted(C); per = {}; mods = {}
for i, tf in enumerate(tfiles):
    fs = set()
    for f, l in C[tf]:
        per.setdefault(f, {}).setdefault(l, set()).add(i); fs.add(f)
    mods[tf] = sorted(fs)
groups = []; gid = {}; pl = {}
for f, d in per.items():
    pl[f] = {}
    for l, s in d.items():
        k = tuple(sorted(s))
        if k not in gid: gid[k] = len(groups); groups.append(k)
        pl[f][l] = gid[k]
out = {"core": {f: sorted(v) for f, v in core.items()}, "tfiles": tfiles, "lines": pl, "groups": groups, "mods": mods}
open(sys.argv[2], "wb").write(gzip.compress(pickle.dumps(out, protocol=4), 9, mtime=0))
print(len(tfiles), "test modules,", sum(map(len, core.values())), "core lines")
