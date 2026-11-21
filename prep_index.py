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
