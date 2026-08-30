"""Extract EVERY record from the gamedata into Records/ for searchable access.

The gamedata (Data/gamedata, gzipped) is a single concatenation of sections
delimited by:

    $$$---$$$ <SectionName> $$--$$ { record } $$--$$ { record } ...

We split it into sections, split each section into its records, and write one
JSON Lines file per section:

    Records/<SectionName>.jsonl

Each line is `json.dumps(record_text)` — the RAW TDF/JSON-ish record text as a
JSON string, so every line is valid JSON and grep-able. A companion
Records/_index.md lists the sections + record counts.

Run from the repo root:
    python3 AssetExtraction/extract_records.py
Override the gamedata path with GAMEDATA=, output dir with RECORDS_DIR=.
"""

import gzip
import json
import os
import re

GAMEDATA = os.environ.get(
    "GAMEDATA",
    "/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE/Data/gamedata",
)
RECORDS_DIR = os.environ.get("RECORDS_DIR", "Records")

_SECTION = re.compile(
    re.escape("$$$---$$$") + r"\s*([A-Za-z0-9_]+)\s*" + re.escape("$$--$$")
)
_RECORD = re.compile(r"\$\$--\$\$")


def main():
    with gzip.open(GAMEDATA, "rb") as f:
        data = f.read().decode("utf-8", "replace")

    os.makedirs(RECORDS_DIR, exist_ok=True)

    segs = [(m.group(1), m.start()) for m in _SECTION.finditer(data)]
    segs.append(("__EOF__", len(data)))

    summary = []
    total = 0
    for i in range(len(segs) - 1):
        name, start = segs[i]
        end = segs[i + 1][1]
        sec = data[start:end]
        # Records are separated by $$--$$; drop empty pieces.
        parts = [p.strip() for p in _RECORD.split(sec) if p.strip()]
        path = os.path.join(RECORDS_DIR, name + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for rec in parts:
                f.write(json.dumps(rec) + "\n")
        summary.append((name, len(parts), end - start))
        total += len(parts)
        print(f"  {name:35s} {len(parts):6d} records -> {path}")

    with open(os.path.join(RECORDS_DIR, "_index.md"), "w", encoding="utf-8") as f:
        f.write("# Gamedata Records\n\n")
        f.write(f"Extracted from `{GAMEDATA}` — {total} records across "
                f"{len(summary)} sections.\n\n")
        f.write("Each section is a JSON Lines file; every line is the raw\n"
                "record text stored as a JSON string (`json.dumps`).\n\n")
        f.write("| Section | Records | Bytes |\n|---------|--------:|------:|\n")
        for name, n, b in summary:
            f.write(f"| {name} | {n} | {b} |\n")

    print(f"\nTotal: {total} records in {len(summary)} sections under {RECORDS_DIR}/")


if __name__ == "__main__":
    main()
