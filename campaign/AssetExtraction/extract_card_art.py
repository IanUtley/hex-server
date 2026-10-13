#!/usr/bin/env python3
"""Extract card art thumbnails for the campaign editor.

Card art lives in the client's ``AssetBundles/cardsets/*.harc`` archives (the
``Harc`` container: ``HARC`` + ``offset,size,path`` lines + blank line + data;
each item is a Unity bundle ``./<art number>.ab``). Writes
``campaign_editor/generated/cards/<art number>.jpg``
plus ``index.json``
(``{card guid: art number}``) for the cards the editor offers (base cards and
alternate-art printings, never the equipment-modified variants). Output is
derived from client assets and is git-ignored.

    python3 campaign/AssetExtraction/extract_card_art.py [--client-root DIR] [--size 128]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from pathlib import Path

import UnityPy  # optional extraction dependency

CAMPAIGN_ROOT = Path(__file__).resolve().parents[1]
ROOT = CAMPAIGN_ROOT.parent
DEFAULT_CLIENT = Path("/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE")
OUT = ROOT / "campaign_editor" / "generated" / "cards"


def card_art() -> dict[str, str]:
    """``{card guid: art number}`` for non-equipment-modified card templates."""
    sys.path[:0] = [str(CAMPAIGN_ROOT),
                    str(ROOT / "campaign_editor")]
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


_RENDER_LOCK = threading.Lock()


def harc_locations(client_root: Path) -> dict[str, tuple[Path, int, int, int]]:
    """``{art number: (harc, data start, offset, size)}`` from the cardset
    headers alone; no art is decoded."""
    locations: dict[str, tuple[Path, int, int, int]] = {}
    for harc in sorted((client_root / "AssetBundles" / "cardsets").glob("*.harc")):
        start, items = harc_index(harc)
        for name, (offset, size) in items.items():
            if re.fullmatch(r"a\d{7}", name):
                locations.setdefault(name, (harc, start, offset, size))
    return locations


def extract_card(locations: dict[str, tuple[Path, int, int, int]], art: str,
                 size: int = 128, out: Path = OUT) -> Path | None:
    """Decode one card's art to ``out/<art>.jpg`` (once; then it is a file).

    Returns the path, or ``None`` when the client has no usable art for it.
    The write goes to a temporary file first, so a reader never sees a half
    image.
    """
    target = out / f"{art}.jpg"
    if target.is_file():
        return target
    location = locations.get(art)
    if location is None:
        return None
    harc, start, offset, span = location
    with _RENDER_LOCK:
        if target.is_file():
            return target
        try:
            with harc.open("rb") as handle:
                handle.seek(start + offset)
                env = UnityPy.load(handle.read(span))
                texture = next(o.read() for o in env.objects
                               if o.type.name == "Texture2D")
                image = texture.image.convert("RGB")
        except Exception as exc:        # a bundle without usable art
            print(f"skip {harc.name}/{art}: {exc}")
            return None
        image.thumbnail((size, size))
        out.mkdir(parents=True, exist_ok=True)
        temporary = out / f".{art}.{os.getpid()}.tmp"
        image.save(temporary, format="JPEG", quality=88)
        os.replace(temporary, target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-root", type=Path, default=DEFAULT_CLIENT)
    parser.add_argument("--size", type=int, default=128)
    parser.add_argument("--all", action="store_true",
                        help="decode every card now (otherwise the editor decodes "
                             "each card the first time it is shown)")
    args = parser.parse_args()
    art = card_art()
    locations = harc_locations(args.client_root)
    OUT.mkdir(parents=True, exist_ok=True)
    index = {guid: number for guid, number in art.items() if number in locations}
    (OUT / "index.json").write_text(json.dumps(index, indent=0), encoding="utf-8")
    print(f"{len(index)}/{len(art)} cards have art in the client")
    if args.all:
        done = 0
        for number in sorted(set(index.values())):
            if extract_card(locations, number, args.size) is not None:
                done += 1
        print(f"{done} card images rendered")


if __name__ == "__main__":
    main()
