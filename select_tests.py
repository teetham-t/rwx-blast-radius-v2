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
    files = {}; cur = None; o = 0; prev_path = None; git_path = None
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
            cur = None
            md = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            git_path = md.group(2) if md else None
            if git_path:
                files.setdefault(git_path, _new_entry())  # also binary / mode-only changes (no hunks)
            continue
        if line.startswith("deleted file mode") and git_path:
            files.setdefault(git_path, _new_entry())["deleted"] = True
            continue
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


_WORDS = None
_WORD_RE = re.compile(r"\w+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def _words():
    """word -> mask indexes built at image build (prep_index.py); False when absent"""
    global _WORDS
    if _WORDS is None:
        p = os.path.join(HERE, "words.pkl")
        try:
            _WORDS = pickle.loads(open(p, "rb").read()) if os.path.exists(p) else False
        except Exception:
            _WORDS = False
    return _WORDS


def source_lines_mentioning(names):
    out = 0
    if not names: return 0
    W = _words()
    if W and all(_WORD_RE.fullmatch(n) for n in names):
        ic = W["ident_cov"]
        for n in names: out |= ic.get(n, 0)
        return out
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
    W = _words()
    if W:
        tw = W["test_words"]
        rest = []
        for t in toks:
            if _ASCII_WORD_RE.fullmatch(t): out |= tw.get(t.lower(), 0)
            else: rest.append(t)
        toks = rest
        if not toks: return out
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


def _public_names(mod):
    src = _src(mod.replace(".", "/") + ".py") or _src(mod.replace(".", "/") + "/__init__.py") or ""
    m = re.search(r"^__all__\s*=\s*[\[(]([^\])]*)", src, re.M)
    if m:
        return re.findall(r"[\"']([A-Za-z_]\w*)[\"']", m.group(1))
    return sorted({x for x in re.findall(r"^(?:class|def)\s+([A-Za-z]\w*)|^([A-Za-z]\w*)\s*[:=]", src, re.M) for x in x if x})


def _reexport_broken(f, names):
    """True when another module (or test) imports one of `names` from module f, or reads it as an
    attribute of f (`exp.DateAdd` after `from sqlglot import expressions as exp`)"""
    if not names or not f.endswith(".py"):
        return False
    mod = f[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[:-9]
    parent, _, last = mod.rpartition(".")
    alt = "|".join(map(re.escape, sorted(set(names))))
    from_mod = re.compile(r"^\s*from\s+%s\s+import\s+(\([^)]*\)|[^\n]*)" % re.escape(mod), re.M)
    name_re = re.compile(r"\b(%s)\b" % alt)
    aliases = set()
    for g, src in _load_src()["src"].items():
        if g == f:
            continue
        for m in from_mod.finditer(src):
            if name_re.search(m.group(1)):
                return True
        if parent:
            for m in re.finditer(r"^\s*from\s+%s\s+import\s+(\([^)]*\)|[^\n]*)" % re.escape(parent), src, re.M):
                for part in m.group(1).strip("()").split(","):
                    bits = part.split("#")[0].split(" as ")
                    if bits[0].strip() == last:
                        aliases.add(bits[-1].strip())
        for m in re.finditer(r"^\s*import\s+%s(?:\s+as\s+(\w+))?" % re.escape(mod), src, re.M):
            aliases.add(m.group(1) or mod)
    if not aliases:
        return False
    use = re.compile(r"\b(%s)\.(%s)\b" % ("|".join(map(re.escape, sorted(aliases))), alt))
    return any(use.search(src) for g, src in _load_src()["src"].items() if g != f)


def select_mask(patch_text, files=None):
    if files is None:
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
            sel |= _unimportable_mask(f); continue
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
            sig = [x for x in _load_src().get("sigs", {}).get(f, ())
                   if (x[0] < ln <= x[1] + 1 if is_ins else x[0] <= ln <= x[1])]
            if sig:
                # a signature line (parameters, defaults, decorators): every test that ran the function
                a0, _, b0 = min(sig, key=lambda x: x[2] - x[0])
                for l2 in range(a0, b0 + 1): sel |= lm.get(l2, 0)
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
                gone = [n for n in names if n != "*" and not re.search(r"\b%s\b" % re.escape(n), added)]
                star = re.match(r"\s*from\s+([\w.]+)\s+import\s+\*", txt)
                if star and not re.search(r"^\s*from\s+%s\s+import\s+\*" % re.escape(star.group(1)), added, re.M):
                    gone += _public_names(star.group(1))
                if gone:
                    sel |= lines_in_file_mentioning(f, gone)
                    if _reexport_broken(f, gone):
                        sel |= _everything()  # other modules take these names from this one
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
            if inner and inner[2] == "attr" and inner[3][0].rsplit(".", 1)[-1] in ENUMS:
                sel |= _enum_member_mask(f, ln, txt, is_ins, info, inner[3][0], inner[3][1])
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
                sel |= source_lines_mentioning(sorted(_derived_names(f, mn.group(1))))
            else:
                enclosing_assign = [x for x in MODULE_ASSIGNS.get(f, ()) if x[0] <= ln <= x[1]]
                if enclosing_assign:
                    key = key_of_text(stripped)
                    if key:
                        # one entry of a module-level table (a registry, say): what uses that key
                        sel |= source_lines_mentioning([enclosing_assign[-1][2]]) | _key_users(key)
                    else:
                        sel |= source_lines_mentioning(sorted(_derived_names(f, enclosing_assign[-1][2])))
                else:
                    sel |= lm.get(ln, 0)
    return sel


def _derived_names(f, name):
    """name plus module-level names in f computed from it (MODULE_BY_X = {... for n in X}), transitively:
    their values are fixed at import, so only the code that reads them later shows in coverage"""
    names = {name}; src = (_src(f) or "").split("\n")
    grew = True
    while grew and src:
        grew = False
        pat = re.compile(r"\b(" + "|".join(map(re.escape, sorted(names))) + r")\b")
        for a, b, n in MODULE_ASSIGNS.get(f, ()):
            if n in names: continue
            text = "\n".join(src[a - 1:b])
            rhs = text.split("=", 1)[1] if "=" in text else text
            if pat.search(rhs):
                names.add(n); grew = True
    return names


def _key_users(key):
    m = tests_mentioning([key])
    for c, cm in CLS_USE.items():
        if c.rsplit(".", 1)[-1] == key: m |= cm
    return m


def _enum_member_mask(f, ln, txt, is_ins, info, cq, attr):
    """an enum member: removing or renaming one that other code references breaks every module that
    names it (at import); a new value changes lookups by value (Dialects.DORIS = "doris")"""
    new = None if is_ins else info["pair"].get(ln)
    if not is_ins and (new is None or not re.match(r"\s*%s\s*[:=]" % re.escape(attr), new)):
        pat = re.compile(r"\.%s\b" % re.escape(attr))
        if any(pat.search(t) for t in _load_src()["src"].values()):
            return _everything()
    vals = [a or b for a, b in STR.findall(txt + "\n" + (new or ""))]
    m = tests_mentioning(vals + [attr]) | source_lines_mentioning([attr]) | class_use(cq)
    low = {v.lower() for v in vals} | {attr.lower()}
    for c, cm in CLS_USE.items():
        if c.rsplit(".", 1)[-1].lower() in low: m |= cm  # Dialects.DORIS = "doris" -> the Doris dialect
    return m


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


_POS = None


def _pos():
    global _POS
    if _POS is None:
        _POS = {t: i for i, t in enumerate(TESTS)}
    return _POS


def _ids_where(pred):
    m = 0
    pos = _pos()
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


_VERDICTS = {}


def file_verdict(f, info):
    key = (f, id(info))
    if key not in _VERDICTS:
        _VERDICTS[key] = _file_verdict(f, info)
    return _VERDICTS[key]


def _file_verdict(f, info):
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


def _unimportable_mask(f):
    """f no longer imports: everything when `import sqlglot` loads it, else what uses f's module or a
    module importing it, plus whole test files whose collection imports one of them"""
    C = _collect()
    if not C or f in C["core"] or any(g in C["core"] for g in _module_importers(f)):
        return _everything()
    mods = _module_importers(f)
    tfs = {tf for tf, fs in C["mods"].items() if any(g in mods for g in fs)}
    m = _module_broken_mask(f)
    if tfs:
        m |= _ids_where(lambda t: t.split("::")[0] in tfs)
    return m


_UNNUMBERED = None


def _unnumbered():
    """test ids in tests.json the index does not number"""
    global _UNNUMBERED
    if _UNNUMBERED is None:
        have = set(TESTS)
        _UNNUMBERED = {t for t in _all_ids() if t not in have}
    return set(_UNNUMBERED)


def _everything():
    EXTRA_IDS.update(_unnumbered())
    return (1 << len(TESTS)) - 1


# ---- import-time failures: lines that run while `import sqlglot` or a test module's collection runs ----
_COLLECT = None
SG_ROOT = os.environ.get("BR_SG_ROOT", os.path.join(HERE, "sg"))  # pinned sqlglot, precompiled at image build


def _collect():
    global _COLLECT
    if _COLLECT is None:
        try:
            _COLLECT = pickle.loads(gzip.decompress(open(os.path.join(HERE, "collect_map.pkl.gz"), "rb").read()))
            _COLLECT["core"] = {f: set(v) for f, v in _COLLECT["core"].items()}
        except Exception:
            _COLLECT = False
    return _COLLECT


def _ids_where_pure(pred):
    m = 0; extra = set()
    pos = _pos()
    for t in _all_ids():
        if pred(t):
            if t in pos: m |= 1 << pos[t]
            else: extra.add(t)
    return m, extra


def _mod_name(f):
    m = f[:-3].replace("/", ".")
    return m[:-9] if m.endswith(".__init__") else m


_HELPER = None  # (pid, job write fd, result read fd) of a forked process holding a clean `import sqlglot`


def _install_finder(table):
    import importlib.abc, importlib.util

    class _F(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            p = table.get(name)
            if p is None:
                return None
            if p.endswith("__init__.py"):
                return importlib.util.spec_from_file_location(
                    name, p, submodule_search_locations=[os.path.join(SG_ROOT, *name.split("."))])
            return importlib.util.spec_from_file_location(name, p)

    sys.meta_path.insert(0, _F())


def _child_import(table, mods):
    """in a forked child: import mods with the patched files swapped in (or, for mods == ["@tpcds"],
    check the TPC-DS optimize hashes); exits 0 / 1, never returns"""
    try:
        import signal
        signal.alarm(30 if mods != ["@tpcds"] else 60)
        dn = os.open(os.devnull, os.O_WRONLY)
        os.dup2(dn, 1); os.dup2(dn, 2)
        import importlib
        table.update(_child_import.pending)
        if mods == ["@tpcds"]:
            g = _gate()
            sys.path.insert(1, HERE)
            import tpcds_gate_child
            ok = tpcds_gate_child.check(g, _child_import.stages, _child_import.only)
            os._exit(0 if ok else 1)
        for m in mods:
            importlib.import_module(m)
        os._exit(0)
    except BaseException:
        os._exit(1)


_child_import.pending = {}
_child_import.only = None
_child_import.stages = "o"


def _wait_fail(pid):
    _, st = os.waitpid(pid, 0)
    return not (os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0)


def _helper():
    """fork a helper that imports the pinned sqlglot once and forks a fresh copy per job"""
    global _HELPER
    if _HELPER is None:
        import gc
        gc.collect(); gc.freeze()
        jr, jw = os.pipe(); rr, rw = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(jw); os.close(rr)
                dn = os.open(os.devnull, os.O_WRONLY); os.dup2(dn, 1); os.dup2(dn, 2)
                table = {}
                import unittest  # noqa: F401  sqlglot turns on extra validation (UNITTEST) under a test runner
                sys.path.insert(0, SG_ROOT); _install_finder(table)
                import sqlglot  # noqa: F401
                import gc as _gc, json as _j
                _gc.collect(); _gc.freeze()
                fin = os.fdopen(jr, "r")
                for line in fin:
                    job = _j.loads(line)
                    c = os.fork()
                    if c == 0:
                        _child_import.pending = job["patched"]
                        _child_import.only = set(job["only"]) if job.get("only") is not None else None
                        _child_import.stages = job.get("stages", "o")
                        _child_import(table, job["mods"])
                    os.write(rw, b"1" if _wait_fail(c) else b"0")
            finally:
                os._exit(0)
        os.close(jr); os.close(rw)
        _HELPER = (pid, jw, rr)
    return _HELPER


def shutdown_helper():
    global _HELPER
    if _HELPER is not None:
        pid, jw, rr = _HELPER
        _HELPER = None
        try:
            os.close(jw); os.waitpid(pid, 0)
        except Exception:
            pass


def _imports_fail(patched, mods):
    """import `mods` with the patched files swapped in, in a forked child; True when that raises (or hangs).
    Patches to modules `import sqlglot` never loads reuse a helper that already imported it."""
    import tempfile, shutil, json as j
    if not os.path.isdir(SG_ROOT):
        return True  # cannot check: assume the worst
    tmp = tempfile.mkdtemp(prefix="br")
    pm = {}
    try:
        for f, text in patched.items():
            dst = os.path.join(tmp, f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(text)
            pm[_mod_name(f)] = dst
        C = _collect()
        core = C and any(f in C["core"] for f in patched)
        if not core and C:
            pid, jw, rr = _helper()
            only = _child_import.only
            os.write(jw, (j.dumps({"patched": pm, "mods": mods, "stages": _child_import.stages,
                                   "only": sorted(only) if only is not None else None}) + "\n").encode())
            return os.read(rr, 1) != b"0"
        import gc
        gc.collect(); gc.freeze()
        c = os.fork()
        if c == 0:
            table = {}
            import unittest  # noqa: F401  (UNITTEST, as under the test runner)
            sys.path.insert(0, SG_ROOT); _install_finder(table)
            _child_import.pending = pm
            _child_import(table, mods)
        return _wait_fail(c)
    except Exception:
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def import_time_mask(files, sel):
    """A change on a line that runs at import (package import or test-module collection) can make the
    import raise, and then every test in the affected modules fails. When the selection does not already
    hold those tests, actually import the patched modules (cheap: everything else is precompiled)."""
    C = _collect()
    if not C:
        return 0
    core_hit = False; tf_idx = set(); patched = {}; module_hit = set()
    for f, info in files.items():
        if not (f.startswith("sqlglot/") and f.endswith(".py")) or info.get("deleted") or info.get("new_file"):
            continue
        try:
            v = file_verdict(f, info)
        except Exception:
            v = None
        if v in ("none", "all"):
            continue
        old = _src(f)
        new = rebuild(old, info) if old is not None else None
        if new is None:
            continue
        patched[f] = new
        pts = set(info["old"]) | set(info["ins"]) | {k - 1 for k in info["ins"]}
        spans = SPANS.get(f, ())
        # a decorator added, or a def line edited: what matters is where the function body runs
        heads = [ln for ln, ts in info["ins"].items() if any(t.strip().startswith("@") for t in ts)]
        heads += [ln for ln, t in info["minus"].items() if re.match(r"\s*(async\s+def|def|@)", t)]
        for h in heads:
            for a, b, _ in spans:
                if h <= a <= h + 3:
                    pts |= set(range(a, b + 1))
        # a line outside every function runs whenever the module is imported
        if any(not any(a < x <= b for a, b, _ in spans) for x in set(info["old"]) | set(info["ins"])):
            module_hit.add(f)
        core = C["core"].get(f, ())
        if any(x in core for x in pts):
            core_hit = True
        lines = C["lines"].get(f, {})
        for x in pts:
            g = lines.get(x)
            if g is not None:
                tf_idx.update(C["groups"][g])
    if not (core_hit or tf_idx or module_hit) or not patched:
        return 0
    tfs = {C["tfiles"][i] for i in tf_idx}
    if core_hit:
        mask, extra = (1 << len(TESTS)) - 1, _unnumbered()
    else:
        mask, extra = _ids_where_pure(lambda t: t.split("::")[0] in tfs)
        for f in patched:
            mask |= _module_broken_mask(f)
    if not (mask & ~sel) and extra <= EXTRA_IDS:
        return 0  # nothing to add
    mods = ["sqlglot"] if core_hit else []
    for tf in sorted(tfs):
        mods += [_mod_name(x) for x in C["mods"].get(tf, ()) if _mod_name(x) not in mods]
    mods += [_mod_name(f) for f in sorted(module_hit) if _mod_name(f) not in mods]
    if _imports_fail(patched, mods):
        EXTRA_IDS.update(extra)
        return mask
    return 0


_IMPORTERS = {}


def _module_importers(f):
    """sqlglot modules importing f's module, transitively (f included): graph built at image build"""
    if f in _IMPORTERS:
        return _IMPORTERS[f]
    graph = _load_src().get("importers")
    if graph is None:
        return {f}
    seen = {f}; stack = [f]
    while stack:
        for h in graph.get(stack.pop(), ()):
            if h not in seen:
                seen.add(h); stack.append(h)
    _IMPORTERS[f] = seen
    return seen


def _module_broken_mask(f):
    """everything that runs code from f's module or a module importing it, or uses their classes"""
    m = 0
    for g in _module_importers(f):
        for x in LINEMAP.get(g, {}).values(): m |= x
        mod = _mod_name(g)
        for c, cm in CLS_USE.items():
            if c.startswith(mod + ".") and "." not in c[len(mod) + 1:]: m |= cm
    return m


# ---- test_execute_tpcds (21 s): drop it when the patch can only change what optimize() returns, and
# optimize() returns the same trees for every TPC-DS query the test executes ----
TPCDS_EXEC = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
_DUCK_PREFIXES = ("sqlglot.generators.duckdb.", "sqlglot.dialects.duckdb.", "sqlglot.parsers.duckdb.",
                  "sqlglot.tokenizers.duckdb.")
_GATE = None
_GATE_SPENT = [0.0]
GATE_BUDGET = 45.0  # seconds of gate checks per batch at most (the batch has 3 minutes)


def _gate():
    global _GATE
    if _GATE is None:
        try:
            import json as j
            g = j.loads(gzip.decompress(open(os.path.join(HERE, "tpcds_gate.json.gz"), "rb").read()))
            g["outside"] = {f: set(v) for f, v in g["outside"].items()}
            g["base"] = j.load(open(os.path.join(HERE, "tpcds_base.json")))
            if any(len(g["base"][k]) != len(g["queries"]) for k in ("opt", "duck")) or \
                    len(g["base"]["parse"]) != len(g["all_queries"]):
                raise ValueError
            _GATE = g
        except Exception:
            _GATE = False
    return _GATE


def tpcds_gate_drops(files):
    """True when every change sits inside function bodies that test_execute_tpcds runs only inside
    optimize() (not in its setup, parsing, planning, execution or duckdb comparison), and optimize()
    still returns identical trees (classes, args, types) for every query the test executes"""
    G = _gate()
    if not G or _GATE_SPENT[0] > GATE_BUDGET:
        return False
    patched = {}; need_stages = set()
    for f, info in files.items():
        if not (f.startswith("sqlglot/") and f.endswith(".py")) or info.get("deleted") or info.get("new_file"):
            return False
        v = file_verdict(f, info)
        if v == "none":
            continue
        if v == "all":
            return False
        spans = SPANS.get(f, ())
        if not all(any(a < x <= b for a, b, _ in spans) for x in set(info["old"]) | set(info["ins"])):
            return False
        st = G.get("stages", {}).get(f, {})
        # the stage trace starts after imports: a line that runs at import (package import, the executor
        # tests' collection, class construction) is invisible to it, so it blocks the gate
        C = _collect(); CB = _classbuild()
        if not C or not CB:
            return False
        imp = set(C["core"].get(f, ()))
        if "tests/test_executor.py" in C["tfiles"]:
            ti = C["tfiles"].index("tests/test_executor.py")
            imp |= {l for l, g in C["lines"].get(f, {}).items() if ti in C["groups"][g]}
        cbl = CB["lines"].get(f, {})
        for x in set(info["old"]) | set(info["ins"]) | {k - 1 for k in info["ins"]}:
            if x in imp:
                return False
            if x in cbl:
                built = CB["groups"][cbl[x]]
                # building DuckDB classes only feeds the duckdb side, which the child re-imports patched
                if not all(k.startswith(_DUCK_PREFIXES) for k in built):
                    return False
                need_stages.add("d")
        for x in set(info["old"]) | set(info["ins"]) | {k - 1 for k in info["ins"]}:
            code = st.get(str(x), "")
            if "x" in code:
                return False  # runs in setup, planning / execution or test code
            need_stages.update(code)
        new = rebuild(_src(f), info)
        if new is None:
            return False
        patched[f] = new
    if not patched:
        return False
    per = G.get("per_query")
    only = None
    if per:
        m = 0
        for f, info in files.items():
            pf = per.get(f, {})
            for x in set(info["old"]) | set(info["ins"]) | {k - 1 for k in info["ins"]}:
                m |= pf.get(str(x), 0)
        only = {i for i in range(len(G["queries"])) if m >> i & 1}
        ins = G.get("inside", {}); stg = G.get("stages", {})
        for f, info in files.items():
            pf = per.get(f, {}); inf = set(ins.get(f, ())); sf = stg.get(f, {})
            if any((x in inf or "o" in sf.get(str(x), "")) and str(x) not in pf
                   for x in set(info["old"]) | set(info["ins"]) | {k - 1 for k in info["ins"]}):
                only = None  # runs only warm (after another query filled a cache): check every query
                break
        if only is not None and not only:
            need_stages.discard("o")  # no query's optimize() runs a changed line
    if not need_stages:
        return True  # no stage of the test runs a changed line: it cannot change
    import time
    t0 = time.time()
    try:
        _child_import.only = only; _child_import.stages = "".join(sorted(need_stages))
        return not _imports_fail(patched, ["@tpcds"])
    finally:
        _child_import.only = None; _child_import.stages = "o"
        _GATE_SPENT[0] += time.time() - t0


# ---- class construction: function bodies that run while classes are built at import (metaclass __new__,
# __init_subclass__, helpers called from class bodies). Coverage credits them to whichever test imported
# first, but what they compute lives on the classes: select the users of those classes ----
_CLASSBUILD = None


def _classbuild():
    global _CLASSBUILD
    if _CLASSBUILD is None:
        try:
            _CLASSBUILD = pickle.loads(gzip.decompress(open(os.path.join(HERE, "classbuild_map.pkl.gz"), "rb").read()))
        except Exception:
            _CLASSBUILD = False
    return _CLASSBUILD


def classbuild_mask(files):
    CB = _classbuild()
    if not CB:
        return 0
    classes = set()
    for f, info in files.items():
        lines = CB["lines"].get(f)
        if not lines or info.get("deleted") or info.get("new_file"):
            continue
        try:
            v = file_verdict(f, info)
        except Exception:
            v = None
        if v == "none":
            continue
        for x in set(info["old"]) | set(info["ins"]) | {k - 1 for k in info["ins"]}:
            g = lines.get(x)
            if g is not None:
                classes.update(CB["groups"][g])
    m = 0
    for k in classes:
        m |= class_use(k)
        if k in EXPR_CLASSES:
            m |= expr_mask(k)
    return m


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
    EXTRA_IDS.clear(); _VERDICTS.clear()
    files = parse_patch(patch_text)
    sel = select_mask(patch_text, files)
    try:
        sel |= classbuild_mask(files)
    except Exception:
        pass
    widened = 0
    try:
        widened = import_time_mask(files, sel)
        sel |= widened
    except Exception:
        widened = -1
    res = [TESTS[i] for i in range(len(TESTS)) if sel >> i & 1]
    if TPCDS_EXEC in res and not widened:
        try:
            if tpcds_gate_drops(files):
                res.remove(TPCDS_EXEC)
        except Exception:
            pass
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
