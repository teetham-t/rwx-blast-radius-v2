"""Entry point: read /data, print one JSON line per commit."""
import json, os, sys, time
t0 = time.process_time()
DATA = os.environ.get("BR_DATA", "/data")
import select_tests as S

def split(items, k):
    ws = [[] for _ in range(k)]; loads = [0] * k
    for ms, n in sorted(items, reverse=True):
        j = loads.index(min(loads)); ws[j].append(n); loads[j] += ms
    return ws, (max(loads) if items else 0)

def best_split(items):
    if not items: return []
    best = None
    for k in range(1, 9):
        ws, mx = split(items, k)
        cost = mx + 2000 * k
        if best is None or cost < best[0]: best = (cost, ws)
    return [w for w in best[1] if w]

def main():
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    by_id = {t["id"]: t for t in tests}
    S.ALL_IDS = [t["id"] for t in tests]
    zero_ms = [t["number"] for t in tests if round(t["seconds"] * 1000) == 0]
    cdir = os.path.join(DATA, "commits")
    out = []
    for fn in sorted(os.listdir(cdir)):
        if not fn.endswith(".patch"): continue
        cid = fn[:-6]
        try:
            sel = S.select(open(os.path.join(cdir, fn), encoding="utf-8", errors="replace").read())
        except Exception as e:  # never crash: fall back to everything
            print(f"{cid}: selector error {e!r}", file=sys.stderr)
            sel = [t["id"] for t in tests]
        items = [(round(by_id[t]["seconds"] * 1000), by_id[t]["number"]) for t in sel if t in by_id]
        workers = best_split(items)
        if workers:
            # tests recorded at 0 ms cost nothing once a worker exists: free insurance
            have = {n for w in workers for n in w}
            workers[-1].extend(n for n in zero_ms if n not in have)
        out.append(json.dumps({"commit": cid, "workers": workers}))
    try:
        S.shutdown_helper()
    except Exception:
        pass
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    print(f"cpu {time.process_time() - t0:.2f}s", file=sys.stderr)

if __name__ == "__main__":
    main()
