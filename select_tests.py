"""Given a unified diff against the base sqlglot commit, pick the tests likely to fail.

Signals (all precomputed at image build time from the base commit):
  * per-test line coverage           -> changes inside function bodies
  * per-test class-attribute reads   -> changes to scalar class settings (e.g. SUPPORTS_X = True)
  * per-test table-key lookups       -> changes to entries of class-level dicts/sets
                                        (FUNCTIONS, TRANSFORMS, generator dispatch, token sets, ...)
  * test source text                 -> fallback for things the traces cannot see
"""
import os, pickle, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
import gzip
if os.path.exists(os.path.join(HERE, "index.pkl")):  # decompressed + precomputed at image build
    IDX = pickle.loads(open(os.path.join(HERE, "index.pkl"), "rb").read())
else:
    IDX = pickle.loads(gzip.decompress(open(os.path.join(HERE, "index.pkl.gz"), "rb").read()))
REPO = os.environ.get("SQLGLOT_REPO", os.path.join(HERE, "sqlglot"))
TESTS = IDX["tests"]; DURS = IDX["durs"]
LINEMAP = IDX["linemap"]; SPANS = IDX["spans"]
TSRC = IDX["test_src"]; TSPANS = IDX["test_spans"]
READS = IDX["reads"]; KEYS = IDX["keys"]; SUB = IDX["sub"]; CLS_USE = IDX["cls_use"]
STRUCT = IDX["structure"]; EXPR_KEYS = IDX["expr_keys"]
FILES = IDX["files"]; STRSPANS = IDX["strspans"]; STMTS = IDX["stmts"]; COVSRC = IDX["covsrc"]; ODEP = {f: set(v) for f, v in IDX.get("odep", {}).items()}; DOCSPANS = IDX.get("docspans", {})
EXPR_USE = IDX.get("expr_use", {}); EXPR_CLASSES = set(IDX.get("expr_classes", ()))


def expr_mask(cq):
    m = 0
    for d in subtree(cq):
        m |= EXPR_USE.get(d, 0)
    return m


IMPORTS = IDX["imports"]


def lines_in_file_mentioning(f, names):
    out = 0
    src = COVSRC.get(f, {})
    pat = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
    for ln, m in LINEMAP.get(f, {}).items():
        if pat.search(src.get(ln, "")): out |= m
    return out

import ast as _ast
METHOD_COV = IDX.get("_method_cov")
if METHOD_COV is None:
    METHOD_COV = {}
    for _f, _sp in SPANS.items():
        _lm = LINEMAP.get(_f, {})
        for _a, _b, _n in _sp:
            _m = 0
            for _l in range(_a, _b + 1): _m |= _lm.get(_l, 0)
            METHOD_COV[_n] = METHOD_COV.get(_n, 0) | _m
MODULE_ASSIGNS = IDX.get("module_assigns", {})

# index (class, attr) -> list of (key, mask) for quick per-attr lookups
KEYS_BY_ATTR = IDX.get("_keys_by_attr")
if KEYS_BY_ATTR is None:
    KEYS_BY_ATTR = {}
    for (c, a, k), m in KEYS.items():
        KEYS_BY_ATTR.setdefault((c, a), []).append((k, m))


def _new_entry():
    return {"old": set(), "ins": {}, "minus": {}, "plus": [], "pair": {}, "paired_ins": set()}


def parse_patch(text):
    """old line numbers removed ("minus"), lines inserted before old line n ("ins"), and which
    removed line each inserted line replaces ("pair"). Hunk line counts are honoured, so a removed
    line that starts with "-- " (SQL comment) is never taken for a file header."""
    files = {}; cur = None; o = 0; prev_path = None
    rem_old = rem_new = 0; run_minus = []; plus_seen = 0
    for line in text.split("\n"):
        if rem_old > 0 or rem_new > 0:
            if cur is not None:
                if line.startswith("-"):
                    if not run_minus or plus_seen: run_minus = []; plus_seen = 0
                    cur["old"].add(o); cur["minus"][o] = line[1:]; run_minus.append(o); o += 1; rem_old -= 1
                    continue
                if line.startswith("+"):
                    cur["ins"].setdefault(o, []).append(line[1:]); cur["plus"].append(line[1:])
                    if run_minus and plus_seen < len(run_minus):
                        cur["pair"][run_minus[plus_seen]] = line[1:]
                        cur["paired_ins"].add((o, len(cur["ins"][o]) - 1))
                    plus_seen += 1; rem_new -= 1
                    continue
                if line.startswith("\\"):
                    continue
                if line.startswith(" ") or line == "":
                    cur.setdefault("ctx", {})[o] = line[1:]
                    o += 1; rem_old -= 1; rem_new -= 1; run_minus = []; plus_seen = 0
                    continue
            rem_old = rem_new = 0  # malformed hunk: fall through to header parsing
        if line.startswith("diff --git"):
            cur = None; continue
        if line.startswith("rename from ") or line.startswith("copy from "):
            # the old path stops existing (rename): every test that used it may break
            p = line.split(" ", 2)[2].strip()
            if line.startswith("rename"):
                files.setdefault(p, _new_entry())["deleted"] = True
            continue
        if line.startswith("--- "):
            prev_path = line[4:].strip().split("\t")[0]
            prev_path = prev_path[2:] if prev_path.startswith("a/") else prev_path
            continue
        if line.startswith("+++ "):
            p = line[4:].strip().split("\t")[0]
            p = p[2:] if p.startswith("b/") else p
            deleted = p == "/dev/null"
            if deleted:
                p = prev_path  # file deleted: attribute its lines to the old path
            cur = files.setdefault(p, _new_entry())
            cur["deleted"] = deleted or cur.get("deleted", False)
            cur["new_file"] = prev_path == "/dev/null"
            run_minus = []; plus_seen = 0
            continue
        m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
        if m and cur is not None:
            o = int(m.group(1)); run_minus = []; plus_seen = 0
            rem_old = int(m.group(2)) if m.group(2) is not None else 1
            rem_new = int(m.group(4)) if m.group(4) is not None else 1
            if rem_old == 0: o += 1  # "@@ -n,0": insertion after old line n
            continue
    return files


def items_at(f, ln):
    """innermost structural items (class / attr / method) containing old line ln"""
    out = []
    for a, b, kind, info in STRUCT.get(f, ()):
        if a <= ln <= b:
            out.append((a, b, kind, info))
    return out


def subtree(cq):
    return SUB.get(cq, [cq])


IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
STR = re.compile(r"\"([^\"]{2,})\"|'([^']{2,})'")
STOP = set("self cls return None True False exp expression expressions this args kwargs def class "
           "import from elif else pass and not for while with lambda isinstance super TokenType "
           "seq_get build_formatted_time Dialect Parser Generator Tokenizer".split())


ENUMS = set(IDX.get("enums", ()))
_ENUM_ALT = None


def _norm_eq(s):
    global _ENUM_ALT
    if _ENUM_ALT is None:
        names = "|".join(sorted(map(re.escape, ENUMS))) or "NoEnum"
        _ENUM_ALT = re.compile(r"(?:\b(?:exp\.|tokens\.)?(?:%s)\.[A-Z][A-Z0-9_]*\b|\bNone\b)" % names)
    # `x is not None` == `x != None`; `x is Enum.MEMBER` == `x == Enum.MEMBER`
    def sub(m):
        return m.group(0)
    s = re.sub(r"\bis not\s+(?=(%s))" % _ENUM_ALT.pattern, "!= ", s)
    s = re.sub(r"\bis\s+(?=(%s))" % _ENUM_ALT.pattern, "== ", s)
    s = re.sub(r"(%s)\s+is not\b" % _ENUM_ALT.pattern, r"\1 !=", s)
    s = re.sub(r"(%s)\s+is\b" % _ENUM_ALT.pattern, r"\1 ==", s)
    return s


_STRSEG = re.compile(r"(\"(?:[^\"\\\\]|\\\\.)*\"|'(?:[^'\\\\]|\\\\.)*')")


def _canon(s):
    """whitespace-insensitive outside string literals, exact inside them"""
    parts = _STRSEG.split(s.strip())
    return tuple(p if k % 2 else " ".join(p.split()) for k, p in enumerate(parts))


def equivalent(a, b):
    """True when two versions of a line cannot behave differently (identity vs equality
    against None or an enum member, or whitespace between tokens). Indentation and the text
    of string literals must match exactly."""
    if a == b:
        return True
    if len(a) - len(a.lstrip()) != len(b) - len(b.lstrip()) or a[:len(a) - len(a.lstrip())] != b[:len(b) - len(b.lstrip())]:
        return False
    if a.count('"') % 2 or a.count("'") % 2 or b.count('"') % 2 or b.count("'") % 2 or '"""' in a + b or "\'\'\'" in a + b:
        return False  # part of a multi-line string: whitespace is data
    if _canon(a) == _canon(b):
        return True
    return _canon(_norm_eq(a)) == _canon(_norm_eq(b))


def key_of_text(s):
    s = s.strip()
    m = re.match(r"(?:exp|TokenType|t)\.([A-Za-z_][A-Za-z0-9_]*)\s*[:,]?", s)
    if m: return m.group(1)
    m = re.match(r"[\"']([^\"']+)[\"']\s*[:,]", s)
    if m: return m.group(1)
    m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*[:,]", s)
    if m and not s.startswith(("def ", "return", "if ")): return m.group(1)
    return None


def key_mask(cq, attr, key):
    m = 0
    names = {key, key.upper()} if key else set()
    for d in subtree(cq):
        if key is None:
            m |= READS.get((d, attr), 0)
            continue
        for n in names:
            m |= KEYS.get((d, attr, n), 0)
            if attr == "TRANSFORMS":
                m |= KEYS.get((d, "__dispatch__", n), 0)
    return m


def attr_mask(cq, attr):
    m = 0
    for d in subtree(cq):
        m |= READS.get((d, attr), 0)
    return m


def class_use(cq):
    m = 0
    for d in subtree(cq):
        m |= CLS_USE.get(d, 0)
    return m


def source_lines_mentioning(names):
    out = 0
    if not names: return 0
    pat = re.compile(r"\b(" + "|".join(map(re.escape, names)) + r")\b")
    for f, d in LINEMAP.items():
        src = COVSRC.get(f, {})
        for ln, m in d.items():
            if pat.search(src.get(ln, "")): out |= m
    return out


def tests_mentioning(tokens):
    out = 0
    toks = [t for t in tokens if len(t) >= 3]
    if not toks: return 0
    pat = re.compile(r"(?i)(?<![A-Za-z0-9_])(" + "|".join(map(re.escape, toks)) + r")(?![A-Za-z0-9_])")
    for i, s in TSRC.items():
        if pat.search(s): out |= 1 << i
    return out


def fallback(lines, scope_mask=None):
    idents = {w for l in lines for w in IDENT.findall(l.split("#")[0])} - STOP
    strs = {a or b for l in lines for a, b in STR.findall(l)}
    sql_tokens = {s for s in strs if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_ ]*", s)}
    m = source_lines_mentioning(sorted(idents))
    m |= tests_mentioning(sorted(sql_tokens | {i for i in idents if i[:1].isupper()}))
    if scope_mask is not None:
        m &= scope_mask
    return m


def _imported_names(st):
    m = re.match(r"from\s+\S+\s+import\s+\(?([^)#]*)", st)
    if m:
        return [x.split(" as ")[-1].strip() for x in m.group(1).split(",") if x.strip() and x.strip() != "*"]
    m = re.match(r"import\s+([^#]*)", st)
    if m:
        return [x.split(" as ")[-1].strip().split(".")[0] for x in m.group(1).split(",") if x.strip()]
    return []


def _broken_import(st):
    """`from sqlglot.x import y` where the pinned sqlglot has no module x or no top-level y in it"""
    m = re.match(r"from\s+(sqlglot[\w.]*)\s+import\s+\(?([^)#]*)", st)
    if not m:
        return False
    base = m.group(1).replace(".", "/")
    src = _src(base + ".py")
    if src is None:
        src = _src(base + "/__init__.py")
        if src is None:
            return True
        pkg = base
    else:
        pkg = None
    for x in m.group(2).split(","):
        n = x.split(" as ")[0].strip()
        if not n or n == "*":
            continue
        if pkg and (_src(pkg + "/" + n + ".py") is not None or _src(pkg + "/" + n + "/__init__.py") is not None):
            continue
        if not re.search(r"^(?:(?:async\s+)?def|class)\s+%s\b|^%s\s*[:=]|^\s*(?:from\s+\S+\s+)?import\b[^\n]*\b%s\b|^\s+%s,?\s*$|\b%s\b\s*=" % ((re.escape(n),) * 5), src, re.M):
            return True
    return False


def select_mask(patch_text):
    files = parse_patch(patch_text)
    sel = 0
    for f, info in files.items():
        if f.startswith("tests/") and f.endswith(".py") and not f.startswith("tests/fixtures/"):
            sel |= test_file_mask(f, info)
            continue
        if f.startswith("tests/"):
            m = FILES.get(f, 0)
            if not m:
                base = os.path.basename(f).rsplit(".", 1)[0]
                m = tests_mentioning([base])
            sel |= m
            continue
        if not (f.startswith("sqlglot/") and f.endswith(".py")):
            continue
        lm = LINEMAP.get(f, {})
        if info.get("deleted"):
            for m in lm.values(): sel |= m
            mod = f[:-3].replace("/", ".")
            for c, m in CLS_USE.items():
                if c.startswith(mod + "."): sel |= m
            continue
        try:
            verdict = file_verdict(f, info)
        except Exception:
            verdict = None
        if verdict == "all":
            sel |= _everything(); continue
        if verdict == "none":
            continue
        # events: (old line, text, is_insert)
        equiv = {ln for ln, p in info["pair"].items() if equivalent(info["minus"][ln], p)}
        events = [(ln, txt, False) for ln, txt in info["minus"].items() if ln not in equiv]
        for ln, txts in info["ins"].items():
            for j, t in enumerate(txts):
                if (ln, j) in info["paired_ins"]:
                    continue  # the removed line it replaces stands for it
                events.append((ln, t, True))
        # contiguous inserted blocks that define a brand-new def/class/import are inert on their own:
        # their behaviour only matters through call sites, which show up as other changed lines.
        inert = set()
        for ln, txts in info["ins"].items():
            block_kind = None; base_indent = None; in_doc = False
            for j, t in enumerate(txts):
                st = t.strip()
                ind = len(t) - len(t.lstrip())
                if not st:
                    continue
                if block_kind and ind > base_indent:
                    inert.add((ln, j)); continue
                block_kind = None
                if re.match(r"(async\s+)?def\s|class\s|@", st) or st.startswith(("import ", "from ")):
                    block_kind = "def"; base_indent = ind; inert.add((ln, j)); continue
        # docstring / comment lines
        def is_doc(t):
            st = t.strip()
            return st.startswith(("#", '"""', "'''")) or st.endswith('"""')
        for ln, txt, is_ins in events:
            stripped = txt.strip()
            if any(a <= ln <= b for a, b in DOCSPANS.get(f, ())) and (not is_ins or any(a < ln <= b for a, b in DOCSPANS.get(f, ()))):
                continue  # docstring text: nothing reads it
            if any(a < ln <= b for a, b in STRSPANS.get(f, ())):
                # text added/removed inside a multi-line string literal (SQL templates, docstrings)
                for a, b in STRSPANS.get(f, ()):
                    if a < ln <= b:
                        m = 0
                        for l2 in range(a, b + 1): m |= lm.get(l2, 0)
                        enc = [x for x in SPANS.get(f, ()) if x[0] <= a <= x[1]]
                        if not m and enc:
                            for l2 in range(enc[-1][0], enc[-1][1] + 1): m |= lm.get(l2, 0)
                        sel |= m
                        break
                continue
            if not stripped or stripped.startswith("#") or is_doc(txt):
                continue
            if is_ins and (ln, info["ins"][ln].index(txt)) in inert:
                if stripped.startswith("@"):
                    rest = info["ins"][ln][info["ins"][ln].index(txt) + 1:]
                    if not any(re.match(r"\s*(async\s+)?(def|class)\s", t) for t in rest):
                        # a decorator added to an existing def / class: everything that ran it
                        nxt = sorted((a, b) for a, b, _ in SPANS.get(f, ()) if ln <= a <= ln + 3)
                        if nxt:
                            for l2 in range(nxt[0][0], nxt[0][1] + 1): sel |= lm.get(l2, 0)
                        else:
                            its2 = [x for x in items_at(f, ln) if x[2] == "class"]
                            for x in STRUCT.get(f, ()):
                                if x[2] == "class" and ln <= x[0] <= ln + 3:
                                    sel |= class_use(x[3])
                            if its2:
                                sel |= class_use(its2[-1][3])
                        continue
                md = re.match(r"\s*def\s+([A-Za-z_0-9]+)", txt)
                its0 = items_at(f, max(ln - 1, 1))
                cls = [x for x in its0 if x[2] == "class" and x[0] <= ln - 1 and ln <= x[1] + 1]
                if md and cls:
                    cq = cls[-1][3]; name = md.group(1)
                    if name.endswith("_sql") and name[:-4] in EXPR_KEYS:
                        sel |= key_mask(cq, "TRANSFORMS", EXPR_KEYS[name[:-4]])
                    else:
                        # overriding an inherited method: tests that ran the inherited one via this class
                        base_m = METHOD_COV.get(name, 0)
                        sel |= base_m & class_use(cq)
                elif not cls or not txt[:1].isspace():
                    # module level: a new def / class / import can shadow a name the module already uses
                    mc = re.match(r"(?:async\s+)?(?:def|class)\s+([A-Za-z_0-9]+)", stripped)
                    names = [mc.group(1)] if mc else _imported_names(stripped)
                    if names:
                        sel |= lines_in_file_mentioning(f, names)
                        src = _src(f) or ""
                        if any(len(re.findall(r"\b%s\b" % re.escape(n), src)) > 1 for n in names):
                            # also used where coverage cannot see it (class bodies run at import)
                            mod = f[:-3].replace("/", ".")
                            for c, m in CLS_USE.items():
                                if c.startswith(mod + "."): sel |= m
                    if not mc and _broken_import(stripped):
                        # importing a name that does not exist: the module itself fails to import
                        for m in lm.values(): sel |= m
                        mod = f[:-3].replace("/", ".")
                        for c, m in CLS_USE.items():
                            if c.startswith(mod + "."): sel |= m
                continue
            if not is_ins and re.match(r"\s*(async\s+)?def\s|\s*@", txt):
                for a, b, _ in SPANS.get(f, ()):
                    if a <= ln <= b:
                        for l2 in range(a, b + 1): sel |= lm.get(l2, 0)
                        break
                continue
            imp_hits = [x for x in IMPORTS.get(f, ()) if x[3] <= ln <= x[4]] if not is_ins else []
            if imp_hits or (not is_ins and stripped.startswith(("import ", "from "))):
                # a removed or edited import: code in this file still using a dropped name raises NameError
                names = [x[2] for x in imp_hits if x[0] <= ln <= x[1]] or [x[2] for x in imp_hits]
                added = "\n".join(info["plus"])
                gone = [n for n in names if not re.search(r"\b%s\b" % re.escape(n), added)]
                if gone:
                    sel |= lines_in_file_mentioning(f, gone)
                continue
            its = items_at(f, ln if not is_ins else max(ln - 1, 1))
            if is_ins:
                # an insertion sits between old lines ln-1 and ln; use the item containing both if any
                its = [x for x in its if x[0] <= ln - 1 and ln <= x[1] + 1] or items_at(f, ln)
            inner = its[-1] if its else None
            # 1) inside a method / function body: line coverage
            in_func = any(a <= ln <= b for a, b, _ in SPANS.get(f, ()) if a < ln)
            if inner and inner[2] == "method" or in_func:
                cand = [ln] if not is_ins else [ln - 1, ln]
                m = 0
                od = ODEP.get(f, ())
                if any(c in od for c in cand):
                    # runs once (import time, caches, dispatch building): credit goes to whichever test ran
                    # first, so take everything the outermost enclosing function (and its closures) reaches
                    outer = [x for x in SPANS.get(f, ()) if x[0] <= ln <= x[1]]
                    if outer:
                        a0, b0, _ = min(outer, key=lambda x: x[0])
                        for l2 in range(a0, b0 + 1): m |= lm.get(l2, 0)
                for c in cand:
                    m |= lm.get(c, 0)
                    # the whole statement / condition the line belongs to (short-circuited lines are never 'covered')
                    for a, b in STMTS.get(f, ()):
                        if a <= c <= b and b - a < 40:
                            for l2 in range(a, b + 1): m |= lm.get(l2, 0)
                if not m and is_ins:
                    # an insertion between two never-run lines: widen to the enclosing function
                    for a, b, _ in SPANS.get(f, ()):
                        if a <= ln <= b:
                            for l2 in range(a, b + 1): m |= lm.get(l2, 0)
                            break
                if not m and is_ins and inner and inner[2] == "method":
                    m = class_use(inner[3][0]) & fallback([txt])
                sel |= m
                continue
            # 2) class-level attribute
            if inner and inner[2] in ("attr", "class") and (inner[3][0] if inner[2] == "attr" else inner[3]) in EXPR_CLASSES:
                # expression node settings (arg_types, names, flags): every test that builds such a node
                sel |= expr_mask(inner[3][0] if inner[2] == "attr" else inner[3])
                continue
            if inner and inner[2] == "attr":
                cq, attr, ents = inner[3]
                keys = []
                here = [e for e in ents if e[0] <= ln <= e[1]]
                if here and not is_ins and ln in info["pair"]:
                    # which entry did the edit touch? compare the old and new line column by column
                    a_, b_ = txt, info["pair"][ln]
                    i0 = 0
                    while i0 < min(len(a_), len(b_)) and a_[i0] == b_[i0]: i0 += 1
                    j0 = 0
                    while j0 < min(len(a_), len(b_)) - i0 and a_[-1 - j0] == b_[-1 - j0]: j0 += 1
                    lo, hi = i0, len(a_) - j0
                    for e in here:
                        s0 = e[3] if e[0] == ln else 0
                        e0 = e[4] if e[1] == ln else 10 ** 6
                        if s0 <= max(hi, lo + 1) - 1 and lo <= e0:
                            keys.append(e[2])
                    if not keys:
                        keys = [None]  # edit outside every entry (the attribute name, the brackets...)
                elif here:
                    keys = [e[2] for e in here]
                elif is_ins:
                    keys = [key_of_text(stripped)]
                else:
                    keys = [None]
                for key in keys:
                    if key:
                        m = key_mask(cq, attr, key)
                        if not m and not KEYS_BY_ATTR.get((cq, attr)):
                            # table never looked up at runtime (e.g. compiled into a trie at import)
                            m = class_use(cq) & tests_mentioning([key])
                    else:
                        m = attr_mask(cq, attr)
                    sel |= m
                continue
            # 3a) a class header (bases, keywords, name): every test using the class or a subclass
            if re.match(r"\s*class\s", txt):
                hdr = [x for x in STRUCT.get(f, ()) if x[2] == "class" and x[0] <= ln <= x[0] + 3]
                for x in hdr:
                    sel |= class_use(x[3]) | expr_mask(x[3])
                if hdr or not is_ins:
                    if not hdr and inner and inner[2] == "class":
                        sel |= class_use(inner[3])
                    continue
            # 3) new method / attr inserted directly in a class body
            if inner and inner[2] == "class":
                cq = inner[3]
                md = re.match(r"\s*def\s+([A-Za-z_0-9]+)", txt)
                if md:
                    name = md.group(1)
                    if name.endswith("_sql") and name[:-4] in EXPR_KEYS:
                        sel |= key_mask(cq, "TRANSFORMS", EXPR_KEYS[name[:-4]])
                    else:
                        sel |= class_use(cq) & fallback([txt])
                    continue
                ma = re.match(r"\s*([A-Z][A-Z0-9_]+)\s*[:=]", txt)
                if ma:
                    sel |= attr_mask(cq, ma.group(1))
                    continue
                sel |= class_use(cq) & fallback([txt])
                continue
            # 4) module level statement (constant / helper table): tests running lines that use the name
            mn = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]*)?=", txt)
            if mn:
                sel |= source_lines_mentioning([mn.group(1)])
            else:
                enclosing_assign = [x for x in MODULE_ASSIGNS.get(f, ()) if x[0] <= ln <= x[1]]
                if enclosing_assign:
                    sel |= source_lines_mentioning([enclosing_assign[-1][2]])
                else:
                    sel |= lm.get(ln, 0)
    return sel


import json as _json
try:
    TSTRUCT = _json.load(open(os.path.join(HERE, "test_struct.json")))
except Exception:
    TSTRUCT = {}
ALL_IDS = None  # every test id in tests.json, set by main.py
EXTRA_IDS = set()  # ids chosen by file / class outside the bitmask (tests the index does not number)


def _all_ids():
    return ALL_IDS or TESTS


def _importers(f):
    """test modules that import f, transitively (f included)"""
    seen = {f}; stack = [f]
    while stack:
        g = stack.pop()
        pkg = g.endswith("__init__.py") and g[:-len("__init__.py")]
        for h, d in TSTRUCT.items():
            if h in seen: continue
            if g in d["imports"] or (pkg and h.startswith(pkg)):
                seen.add(h); stack.append(h)
    return seen


def _class_family(names):
    """class names plus every test class deriving from them (by base name, any module)"""
    fam = set(names); changed = True
    while changed:
        changed = False
        for d in TSTRUCT.values():
            for c in d["classes"]:
                if c["name"] not in fam and any(b in fam for b in c["bases"]):
                    fam.add(c["name"]); changed = True
    return fam


def _ids_where(pred):
    m = 0
    pos = {t: i for i, t in enumerate(TESTS)}
    for t in _all_ids():
        if pred(t):
            if t in pos: m |= 1 << pos[t]
            else: EXTRA_IDS.add(t)
    return m


def test_file_mask(f, info):
    """A test module edit. Inside a test: that test. Inside a test class but outside its tests
    (setUp, helpers, class settings): every test of that class and its subclasses. Module level
    (imports, helpers, constants): every test in the module and in the modules importing it."""
    st = TSTRUCT.get(f)
    spans = TSPANS.get(f, ())
    events = [(ln, t, False) for ln, t in info["minus"].items()]
    events += [(ln, t, True) for ln, ts in info["ins"].items() for t in ts]
    if info.get("deleted"):
        return _ids_where(lambda t: t.split("::")[0] in _importers(f))
    try:
        verdict = file_verdict(f, info)
    except Exception:
        verdict = None
    if verdict == "none":
        return 0
    if verdict == "all":  # the module cannot be imported: it and its importers fail
        return _ids_where(lambda t: t.split("::")[0] in _importers(f))
    m = 0; fam = set(); module_level = False; defnames = set()
    for ln, txt, is_ins in events:
        hit = [i for a, b, i in spans if (a < ln <= b if is_ins else a <= ln <= b)]
        if hit:
            for i in hit: m |= 1 << i
            continue
        stripped = txt.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if st is None:
            module_level = True; continue
        cls = [c for c in st["classes"] if (c["a"] < ln <= c["b"] + (1 if is_ins else 0) if is_ins else c["a"] <= ln <= c["b"])]
        if cls:
            c = cls[0]
            in_test = [mm for mm in c["methods"] if mm[0].startswith("test") and
                       (mm[1] < ln <= mm[2] if is_ins else mm[1] <= ln <= mm[2])]
            if in_test:
                # a test the index does not number (skipped, renamed...): select it by id
                for mm in in_test:
                    m |= _ids_where(lambda t, n="%s::%s::%s" % (f, c["name"], mm[0]): t.split("[")[0] == n)
                continue
            if is_ins and re.match(r"\s*(async\s+)?def\s+test", txt):
                continue  # a brand-new test is not in tests.json
            fam.add(c["name"])
            continue
        d = [x for x in st.get("defs", ()) if x[1] <= ln <= x[2]]
        if d:
            defnames.add(d[0][0]); continue
        module_level = True
    if defnames and not module_level:
        # a top-level helper / constant: this module, and importers that use the name (and so on)
        def closure(g, ns):
            # names in g whose definition reads a changed name change too
            ns = set(ns); defs = TSTRUCT.get(g, {}).get("defs", ())
            grew = True
            while grew:
                grew = False
                for x in defs:
                    if x[0] not in ns and len(x) > 3 and ns & set(x[3]):
                        ns.add(x[0]); grew = True
            return ns
        files = {f}; frontier = [(f, closure(f, defnames))]
        while frontier:
            g, ns = frontier.pop()
            for h, dh in TSTRUCT.items():
                if h in files or g not in dh["imports"]: continue
                used = ns & set(dh.get("names", ()))
                if used or g.endswith("__init__.py"):
                    files.add(h)
                    # everything h defines may now behave differently
                    frontier.append((h, {x[0] for x in dh.get("defs", ())} | {c["name"] for c in dh["classes"]}))
        m |= _ids_where(lambda t: t.split("::")[0] in files)
    if fam:
        fam = _class_family(fam)
        m |= _ids_where(lambda t: len(t.split("::")) > 2 and t.split("::")[1] in fam)
    if module_level:
        files = _importers(f)
        m |= _ids_where(lambda t: t.split("::")[0] in files)
    return m


# ---- whole-file check against the pinned sources: syntax errors and changes the AST cannot see ----
_SRC = None


def _load_src():
    global _SRC
    if _SRC is None:
        try:
            if os.path.exists(os.path.join(HERE, "src.pkl")):
                _SRC = pickle.loads(open(os.path.join(HERE, "src.pkl"), "rb").read())
            else:
                _SRC = pickle.loads(gzip.decompress(open(os.path.join(HERE, "src.pkl.gz"), "rb").read()))
        except Exception:
            _SRC = {"src": {}, "spans": {}}
    return _SRC


def _src(f):
    return _load_src()["src"].get(f)


def rebuild(old, info):
    """the patched file, or None when the patch does not fit the pinned source"""
    lines = old.split("\n")
    for n, t in list(info["minus"].items()) + list(info.get("ctx", {}).items()):
        if n < 1 or n > len(lines) or lines[n - 1] != t:
            return None
    out = []
    for n, l in enumerate(lines, 1):
        out.extend(info["ins"].get(n, ()))
        if n not in info["old"]:
            out.append(l)
    for n in sorted(k for k in info["ins"] if k > len(lines)):
        out.extend(info["ins"][n])
    return "\n".join(out)


class _Norm(_ast.NodeTransformer):
    """drop docstrings, and signature annotations when they are never evaluated"""

    def __init__(self, strip_ann):
        self.strip_ann = strip_ann

    def _doc(self, node):
        b = node.body
        if b and isinstance(b[0], _ast.Expr) and isinstance(b[0].value, _ast.Constant) and isinstance(b[0].value.value, str):
            node.body = b[1:]

    def visit_Module(self, node):
        self._doc(node); self.generic_visit(node); return node

    def visit_ClassDef(self, node):
        self._doc(node); self.generic_visit(node); return node

    def visit_FunctionDef(self, node):
        self._doc(node)
        if self.strip_ann:
            node.returns = None
        self.generic_visit(node); return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_arg(self, node):
        if self.strip_ann:
            node.annotation = None
        return node


_FUTURE_ANN = re.compile(r"^from __future__ import [^\n]*\bannotations\b", re.M)


def _future_ann(tree):
    return any(isinstance(n, _ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names)
               for n in tree.body)


def _clusters(info, gap=3):
    pts = sorted(set(info["old"]) | set(info["ins"]))
    out = []
    for n in pts:
        if out and n - out[-1][-1] <= gap: out[-1].append(n)
        else: out.append([n])
    return out


def _segment(f, lines, info, lo, hi, limit=600):
    """old and new text (dedented) of the smallest def / class / statement holding the changes at old
    lines lo..hi; None when there is no such span of at most `limit` lines"""
    olds_ = [n for n in info["old"] if lo <= n <= hi]; ins_ = [k for k in info["ins"] if lo <= k <= hi]
    for a, b in _load_src()["spans"].get(f, ()):
        if b - a > limit:
            return None  # spans are sorted smallest first
        # removed lines inside the span (header included); insertions after the header, up to just
        # past its last line (a replaced last line, or lines appended to it)
        if not (all(a <= n <= b for n in olds_) and all(a < k <= b + 1 for k in ins_)):
            continue
        ind = lines[a - 1][:len(lines[a - 1]) - len(lines[a - 1].lstrip())]
        changed = [info["minus"][n] for n in olds_] + [t for k in ins_ for t in info["ins"][k]]
        if any(t.strip() and not t.startswith(ind) for t in changed):
            return None
        news = []
        for n in range(a, b + 1):
            news.extend(info["ins"].get(n, ()))
            if n not in info["old"]:
                news.append(lines[n - 1])
        news.extend(info["ins"].get(b + 1, ()))
        cut = lambda xs: "\n".join(x[len(ind):] if x.startswith(ind) else x for x in xs)
        return cut(lines[a - 1:b]), cut(news)
    return None


def file_verdict(f, info):
    """"all": the patched file no longer parses, so importing it fails; "none": same program
    (comments, formatting, docstrings, unevaluated annotations); None: look at the lines.
    Each cluster of changes is compared inside its smallest enclosing def / statement; the whole
    file is parsed only when that is not possible or a piece does not parse."""
    old = _src(f)
    if old is None or info.get("deleted") or info.get("new_file"):
        return None
    new = rebuild(old, info)
    if new is None:
        return None
    lines = old.split("\n")
    strip = bool(_FUTURE_ANN.search(old))
    same = True; need_full = False
    for cl in _clusters(info):
        seg = _segment(f, lines, info, cl[0], cl[-1])
        if seg is None and not any(n in info["old"] for n in cl):
            # pure insertion between statements (a new method, say): the block alone, at the
            # indentation of the line it lands before
            ins = [t for k in cl for t in info["ins"].get(k, ())]
            body = [t for t in ins if t.strip()]
            nxt = next((lines[n - 1] for n in range(cl[-1], len(lines) + 1) if lines[n - 1].strip()), "")
            ind = nxt[:len(nxt) - len(nxt.lstrip())]
            if body and all(t.startswith(ind) for t in body) and body[0][len(ind):len(ind) + 1] not in (" ", "\t"):
                if all(t.strip().startswith("#") for t in body):
                    continue
                try:
                    _ast.parse("\n".join(t[len(ind):] if t.startswith(ind) else t for t in ins))
                    same = False; continue
                except (SyntaxError, ValueError):
                    pass
        if seg is None:
            need_full = True; same = False; continue
        try:
            t_a = _ast.parse(seg[0]); t_b = _ast.parse(seg[1])
        except (SyntaxError, ValueError):
            need_full = True; same = False; continue
        if _ast.dump(_Norm(strip).visit(t_a)) != _ast.dump(_Norm(strip).visit(t_b)):
            same = False
    if same:
        return "none"
    if need_full:
        try:
            _ast.parse(new)
        except (SyntaxError, ValueError):
            return "all"
    return None


def _everything():
    EXTRA_IDS.update(t for t in _all_ids() if t not in set(TESTS))
    return (1 << len(TESTS)) - 1


LAZY_LOAD = "tests/dialects/test_dialect.py::TestDialect::test_lazy_load"
TYPING_META = "tests/test_typing.py::TestTyping::test_no_redundant_expression_metadata"
SQLGLOTC = ["tests/test_sqlglotc.py::TestSQLGlotC::test_build_removes_obsolete_extensions",
            "tests/test_sqlglotc.py::TestSQLGlotC::test_dry_run_preserves_obsolete_extensions",
            "tests/test_sqlglotc.py::TestSQLGlotC::test_editable_install_removes_obsolete_extensions"]
CORE_IMPORTED = ("sqlglot/__init__.py", "sqlglot/dialects/__init__.py", "sqlglot/dialects/dialect.py",
                 "sqlglot/parsers/__init__.py", "sqlglot/generators/__init__.py")


def extra_tests(patch_text):
    """Tests the maps cannot see (they work in subprocesses or read source files)."""
    out = []
    files = parse_patch(patch_text)
    for f, info in files.items():
        eq = {ln for ln, p in info["pair"].items() if equivalent(info["minus"][ln], p)}
        changed = [l for ln, l in info["minus"].items() if ln not in eq]
        changed += [l for ln, ls in info["ins"].items() for j, l in enumerate(ls)
                    if not any(o in eq and info["pair"][o] == l for o in info["pair"])]
        changed = [l for l in changed if l.strip() and not l.strip().startswith("#")]
        if not changed:
            continue
        if f.startswith("sqlglot/typing/"):
            out.append(TYPING_META)
        if f.startswith("sqlglotc/") or f in ("setup.py", "pyproject.toml", "setup.cfg"):
            out += SQLGLOTC
        # what `import sqlglot` loads eagerly: module-level statements in the core modules, or imports
        module_level = [l for l in changed if re.match(r"[A-Za-z_]", l) and not re.match(r"(def|class|async def)\b", l)]
        imports = [l for l in changed if re.match(r"\s*(import |from \S+ import )", l)]
        if f.startswith("sqlglot/") and (imports or (f in CORE_IMPORTED and module_level)):
            if not f.startswith(("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/tokenizers/", "sqlglot/typing/")) \
                    and not (f.startswith("sqlglot/dialects/") and not f.endswith(("__init__.py", "dialect.py"))):
                out.append(LAZY_LOAD)
    return out


def select(patch_text):
    EXTRA_IDS.clear()
    sel = select_mask(patch_text)
    res = [TESTS[i] for i in range(len(TESTS)) if sel >> i & 1]
    res += [t for t in sorted(EXTRA_IDS) if t not in set(res)]
    try:
        for x in extra_tests(patch_text):
            if x not in res: res.append(x)
    except Exception:
        pass
    return res


if __name__ == "__main__":
    for p in sys.argv[1:]:
        r = select(open(p).read())
        D = dict(zip(TESTS, DURS))
        print(os.path.basename(p), len(r), round(sum(D[t] for t in r), 2))
