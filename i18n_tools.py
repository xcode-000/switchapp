"""Translation helpers: `python app.py i18n-check` lists the texts missing from translations/*.json.

Texts are collected from:
  * Python files: _("..."), N_("..."), tr("...") calls and [tr(h) for h in ["...", ...]] lists
  * templates:    {{ _("...") }}
  * i18n.JS_STRINGS (texts used by the JavaScript files)
"""
import ast
import glob
import json
import os
import re

import i18n

BASE = os.path.dirname(os.path.abspath(__file__))
TPL_RE = re.compile(r"""\b_\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")


def _py_texts(path):
    out = []
    tree = ast.parse(open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("_", "N_", "tr") and node.args \
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            out.append(node.args[0].value)
        if isinstance(node, ast.ListComp) and isinstance(node.elt, ast.Call) and getattr(node.elt.func, "id", None) == "tr":
            for gen in node.generators:
                if isinstance(gen.iter, ast.List):
                    out += [e.value for e in gen.iter.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return out


def collect():
    texts = []
    for py in ("app.py", "importer.py"):
        texts += _py_texts(os.path.join(BASE, py))
    for tpl in sorted(glob.glob(os.path.join(BASE, "templates", "*.html"))):
        for m in TPL_RE.finditer(open(tpl, encoding="utf-8").read()):
            s = m.group(1) if m.group(1) is not None else m.group(2)
            texts.append(s.encode().decode("unicode_escape").encode("latin-1").decode("utf-8") if "\\" in s else s)
    texts += i18n.JS_STRINGS
    seen, out = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def report():
    texts = collect()
    print(f"{len(texts)} texts")
    for code in i18n.LANGUAGES:
        if code == "en":
            continue
        cat = i18n.catalog(code)
        missing = [t for t in texts if t not in cat]
        unused = [k for k in cat if k not in set(texts)]
        print(f"{code}: {len(missing)} missing, {len(unused)} unused")
        for t in missing:
            print("   missing:", json.dumps(t, ensure_ascii=False))
