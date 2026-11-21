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
open(os.path.join(HERE, "src.pkl"), "wb").write(gzip.decompress(open(os.path.join(HERE, "src.pkl.gz"), "rb").read()))
