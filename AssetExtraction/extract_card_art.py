#!/usr/bin/env python3
"""Extract card art thumbnails for the campaign editor.

Card art lives in the client's ``AssetBundles/cardsets/*.harc`` archives (the
``Harc`` container: ``HARC`` + ``offset,size,path`` lines + blank line + data;
each item is a Unity bundle ``./<art number>.ab``). Writes
``tools/campaign_editor/generated/cards/<art number>.jpg`` plus ``index.json``
(``{card guid: art number}``) for the cards the editor offers (base cards and
alternate-art printings, never the equipment-modified variants). Output is
derived from client assets and is git-ignored.

    python3 AssetExtraction/extract_card_art.py [--client-root DIR] [--size 128]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import UnityPy  # optional extraction dependency

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CLIENT = Path("/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE")
OUT = ROOT / "tools" / "campaign_editor" / "generated" / "cards"


def card_art() -> dict[str, str]:
    """``{card guid: art number}`` for non-equipment-modified card templates."""
    sys.path.insert(0, str(ROOT / "tools" / "campaign_editor"))
    import catalog
    art = {}
    for record in catalog.records("CardTemplate"):
        if record.get("m_EquipmentModifiedCard"):
            continue
        number = str(record.get("m_ArtNumber") or "")
        if re.fullmatch(r"a\d{7}", number):
            art[catalog._guid(record.get("m_Id"))] = number
    return art


def harc_index(path: Path) -> tuple[int, dict[str, tuple[int, int]]]:
    """``(data start, {art number: (offset, size)})`` from a .harc header."""
    items, position = {}, 0
    with path.open("rb") as handle:
        if handle.readline() != b"HARC\r\n":
            raise ValueError(f"{path.name}: not a HARC file")
        while True:
            line = handle.readline()
            if line in (b"\r\n", b""):
                break
            offset, size, name = line.decode().rstrip("\r\n").split(",", 2)
            items[Path(name).stem] = (int(offset), int(size))
        return handle.tell(), items


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-root", type=Path, default=DEFAULT_CLIENT)
    parser.add_argument("--size", type=int, default=128)
    args = parser.parse_args()
    art = card_art()
    wanted = set(art.values())
    OUT.mkdir(parents=True, exist_ok=True)
    saved = 0
    for harc in sorted((args.client_root / "AssetBundles" / "cardsets").glob("*.harc")):
        start, items = harc_index(harc)
        todo = [(name, span) for name, span in items.items()
                if name in wanted and not (OUT / f"{name}.jpg").exists()]
        if not todo:
            continue
        with harc.open("rb") as handle:
            for name, (offset, size) in todo:
                handle.seek(start + offset)
                try:
                    env = UnityPy.load(handle.read(size))
                    texture = next(o.read() for o in env.objects
                                   if o.type.name == "Texture2D")
                    image = texture.image.convert("RGB")
                except Exception as exc:        # a bundle without usable art
                    print(f"skip {harc.name}/{name}: {exc}")
                    continue
                image.thumbnail((args.size, args.size))
                image.save(OUT / f"{name}.jpg", quality=88)
                saved += 1
        print(f"{harc.name}: {len(todo)} wanted", flush=True)
    present = {path.stem for path in OUT.glob("a*.jpg")}
    index = {guid: number for guid, number in art.items() if number in present}
    (OUT / "index.json").write_text(json.dumps(index, indent=0), encoding="utf-8")
    print(f"{saved} new images; {len(index)}/{len(art)} cards have art")


if __name__ == "__main__":
    main()
