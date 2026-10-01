#!/usr/bin/env python3
"""Field-level audit of Python effect leaves against the C# effect classes.

Unlike ``sweep_set1_coverage.py`` (which only checks that *a* handler is
registered), this tool compares the typed fields each C# ``*EffectTemplate``
class carries with the fields the Python leaf actually reads.  It reports:

* effect classes present in Records with no Python leaf,
* C# fields that appear in the Records snapshot for a class but are never
  read by that class's Python leaf (candidate semantic gaps),
* fields the Python leaf reads that the Records snapshot does not carry for
  that class (stale/incorrect field names),
* leaves that read no typed ``m_*`` field at all (text-inference risk).

Usage:
    python3 AssetExtraction/audit_effect_leaves.py
    python3 AssetExtraction/audit_effect_leaves.py --json /tmp/leaf_audit.json
"""

import argparse
import ast
import json
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CS_ROOT = os.path.join(ROOT, "HexClient", "Assembly-CSharp-firstpass")
ABILITY_DIR = os.path.join(CS_ROOT, "Game", "Shared", "Mechanics", "Abilities")
BASE_FILES = [
    os.path.join(CS_ROOT, "Reckoning", "Game", "AbilityEffectTemplate.cs"),
    os.path.join(ABILITY_DIR, "CardAbilityEffectTemplate.cs"),
    os.path.join(ABILITY_DIR, "BattleAbilityEffectTemplate.cs"),
]
ABILITIES_DIR = os.path.join(ROOT, "abilities")
PYTHON_DIRS = (ABILITIES_DIR, os.path.join(ROOT, "rules_port"))

FIELD_RE = re.compile(r"^m_[A-Za-z0-9_]+$")

# Fields the Python port deliberately does not consume even though Records
# populates them.  Each entry must cite the C# reason, so this stays a short
# evidence list rather than a suppression mechanism.
ACCEPTED_FIELDS = {
    ("CreateAndCastSpellAbilityEffectTemplate", "m_SendPlayAction"):
        "client presentation flag; AuthoritativeSessionBase."
        "CopyCardAndPutOnChain ignores it server-side",
}
CLASS_DECL_RE = re.compile(
    r"\b(?:public|internal)\s+(?:abstract\s+|sealed\s+|static\s+)*class\s+"
    r"([A-Za-z0-9_`]+)\s*(?::\s*([A-Za-z0-9_\.<>]+))?")
CS_FIELD_RE = re.compile(
    r"^\s*(?:public|protected|internal)\s+"
    r"(?:static\s+|readonly\s+|const\s+|new\s+)*"
    r"[A-Za-z0-9_\.]+(?:\s*<[^;{}]*?>)?(?:\[\])?"
    r"(?:\s*[?])?\s+(m_[A-Za-z0-9_]+)\s*(?:=[^;]*)?;\s*$",
    re.MULTILINE)


def _read(path):
    with open(path, encoding="utf-8-sig", errors="replace") as handle:
        return handle.read()


def load_csharp_classes():
    """Return {class_name: {"base", "fields", "file"}} for effect classes."""
    classes = {}
    files = set(BASE_FILES)
    if os.path.isdir(ABILITY_DIR):
        for name in os.listdir(ABILITY_DIR):
            if name.endswith(".cs"):
                files.add(os.path.join(ABILITY_DIR, name))
    for path in sorted(files):
        if not os.path.isfile(path):
            continue
        text = _read(path)
        decls = list(CLASS_DECL_RE.finditer(text))
        for index, match in enumerate(decls):
            name, base = match.group(1), match.group(2)
            start = match.end()
            end = decls[index + 1].start() if index + 1 < len(decls) else len(text)
            body = text[start:end]
            fields = [field for field in CS_FIELD_RE.findall(body)]
            classes[name] = {
                "base": base,
                "fields": fields,
                "file": os.path.relpath(path, ROOT),
            }
    return classes


def resolve_csharp_fields(classes, name):
    fields, seen = set(), set()
    cursor = name
    while cursor and cursor not in seen:
        seen.add(cursor)
        entry = classes.get(cursor)
        if not entry:
            break
        fields.update(entry["fields"])
        cursor = entry.get("base")
    return fields


_DEFAULT_VALUES = (
    None, "", 0, 0.0, False, "None", "Unknown", "0",
    "00000000-0000-0000-0000-000000000000",
)


def _is_meaningful(value):
    if isinstance(value, dict):
        if str(value.get("_t") or "").endswith("EffectConstant"):
            return _is_meaningful(value.get("m_Value"))
        return True
    if isinstance(value, list):
        return bool(value)
    return value not in _DEFAULT_VALUES


def load_records():
    """Return {short_type: {"count", "fields", "meaningful"}} from Records."""
    from gamedata import DEFAULT_RECORD_STORE
    records = DEFAULT_RECORD_STORE.load("AbilityEffectTemplate")
    classes = {}

    def visit(node, owner):
        if isinstance(node, dict):
            type_name = str(node.get("_t") or "").rsplit(".", 1)[-1]
            if type_name:
                entry = classes.setdefault(
                    type_name, {"count": 0, "fields": Counter(),
                                "meaningful": Counter()})
                for key, value in node.items():
                    if FIELD_RE.match(str(key)):
                        entry["fields"][key] += 1
                        if _is_meaningful(value):
                            entry["meaningful"][key] += 1
            for value in node.values():
                visit(value, type_name or owner)
        elif isinstance(node, list):
            for item in node:
                visit(item, owner)

    for record in records:
        raw = record.raw if isinstance(record.raw, dict) else dict(record)
        concrete = record.type_name.rsplit(".", 1)[-1]
        entry = classes.setdefault(
            concrete, {"count": 0, "fields": Counter(),
                       "meaningful": Counter()})
        entry["count"] += 1
        visit(raw, concrete)
    return classes


def _string_fields(node):
    found = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if FIELD_RE.match(child.value):
                found.add(child.value)
    return found


def _call_names(node):
    found = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                found.add(func.id)
            elif isinstance(func, ast.Attribute):
                found.add(func.attr)
    return found


def load_python_index():
    """Return (functions, methods, all_fields) for abilities/ and rules_port/."""
    functions, methods = {}, {}
    all_fields = set()
    for base_dir in PYTHON_DIRS:
        for dirpath, _, filenames in os.walk(base_dir):
            if "__pycache__" in dirpath:
                continue
            for filename in sorted(filenames):
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                try:
                    tree = ast.parse(_read(path), filename=path)
                except SyntaxError:
                    continue
                all_fields.update(_string_fields(tree))
                for node in ast.walk(tree):
                    if not isinstance(node,
                                      (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    reads = _string_fields(node)
                    calls = _call_names(node)
                    functions.setdefault(node.name, set()).update(reads)
                    functions[node.name].update(calls)
                    if (node.args.args
                            and node.args.args[0].arg in ("self", "cls")):
                        methods.setdefault(node.name, set()).update(reads)
    return functions, methods, all_fields


def python_closure(name, functions, methods, depth=4):
    """Transitively gather m_* reads reachable from a leaf by call name."""
    seen, frontier, reads = set(), {name}, set()
    for _ in range(depth + 1):
        following = set()
        for current in frontier:
            if current in seen:
                continue
            seen.add(current)
            for table in (functions, methods):
                entry = table.get(current)
                if not entry:
                    continue
                for item in entry:
                    if FIELD_RE.match(item):
                        reads.add(item)
                    elif item not in seen:
                        following.add(item)
        frontier = following
    return reads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default=os.path.join(
        ROOT, "docs", "generated", "effect_leaf_audit.json"))
    args = parser.parse_args()

    from abilities.framework.bom import _LEAFS

    classes = load_csharp_classes()
    records = load_records()
    functions, methods, all_fields = load_python_index()

    leaf_names = set(_LEAFS)
    report = {}
    for type_name, info in sorted(records.items()):
        if not type_name.endswith("EffectTemplate"):
            continue
        csharp_fields = resolve_csharp_fields(classes, type_name)
        record_fields = set(info["fields"])
        if type_name in _LEAFS:
            leaf = _LEAFS[type_name]
            func = getattr(leaf, "__wrapped__", leaf)
            reads = python_closure(func.__name__, functions, methods)
        else:
            reads = set()
        relevant_csharp = {
            field for field in csharp_fields
            if field not in {
                "m_TemplateId", "m_Name", "m_EditorVariableMap", "m_GameText",
                "m_SerializedTAC"}}
        meaningful = set(info["meaningful"])
        unread = sorted(
            field for field in relevant_csharp & record_fields
            if field not in reads)
        unread_meaningful = sorted(
            field for field in relevant_csharp & meaningful
            if field not in reads
            and (type_name, field) not in ACCEPTED_FIELDS)
        python_only = sorted(
            field for field in reads if field not in record_fields)
        report[type_name] = {
            "records": info["count"],
            "has_leaf": type_name in _LEAFS,
            "csharp_fields": sorted(relevant_csharp),
            "record_fields": sorted(record_fields),
            "meaningful_fields": {
                field: info["meaningful"][field]
                for field in sorted(info["meaningful"])},
            "leaf_reads": sorted(reads),
            "unread_record_fields": unread,
            "unread_meaningful_fields": unread_meaningful,
            "python_only_fields": python_only,
            "read_elsewhere": sorted(
                field for field in unread if field in all_fields),
        }

    os.makedirs(os.path.dirname(args.json), exist_ok=True)
    with open(args.json, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1, sort_keys=True)

    missing_leaf = [name for name, row in report.items()
                    if not row["has_leaf"] and row["records"]]
    text_only = [name for name, row in report.items()
                 if row["has_leaf"] and not row["leaf_reads"]
                 and (row["meaningful_fields"] or row["csharp_fields"])]
    gaps = {name: row for name, row in report.items()
            if row["unread_meaningful_fields"] and row["has_leaf"]}

    print(f"effect classes in Records: {len(report)}")
    print(f"  no Python leaf:        {len(missing_leaf)}")
    print(f"  leaf reads no m_*:     {len(text_only)}")
    print(f"  classes with meaningful unread fields: {len(gaps)}")
    print()
    for name in missing_leaf:
        print(f"  NO LEAF  {report[name]['records']:5d}  {name}")
    for name in text_only:
        print(f"  TEXT?    {report[name]['records']:5d}  {name}")
    print()
    for name, row in sorted(gaps.items(), key=lambda kv: -kv[1]["records"]):
        unread = row["unread_meaningful_fields"]
        elsewhere = row["read_elsewhere"]
        tag = "" if elsewhere else "  <-- not read anywhere in Python"
        print(f"  {row['records']:5d}  {name}: {', '.join(unread)}{tag}")
    print(f"\nwrote {os.path.relpath(args.json, ROOT)}")


if __name__ == "__main__":
    main()
