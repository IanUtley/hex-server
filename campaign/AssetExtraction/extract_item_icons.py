#!/usr/bin/env python3
"""Extract inventory item icons (equipment, chests/packs, stardust, ...) for the campaign editor.

Reads ``AssetBundles/general/icons.ab`` from the client and writes small PNG
thumbnails to ``campaign_editor/generated/items/<art number>.png`` plus
``items/index.json`` (``{item guid: art number}``) from the Records
``InventoryItemData`` (``m_ArtNumber``). Output is derived from client assets
and is git-ignored.

    python3 campaign/AssetExtraction/extract_item_icons.py [--client-root DIR] [--size 96]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import UnityPy  # optional extraction dependency

CAMPAIGN_ROOT = Path(__file__).resolve().parents[1]
ROOT = CAMPAIGN_ROOT.parent
DEFAULT_CLIENT = Path("/mnt/d/SteamLibrary/steamapps/common/HEX SHARDS OF FATE")
OUT = ROOT / "campaign_editor" / "generated" / "items"
KINDS = ("InventoryEquipmentData", "InventoryTreasureChest", "InventoryStardust",
         "InventoryCardPack", "InventoryStarterPack", "InventoryGemData",
         "InventoryMercenaryData")


def item_art() -> dict[str, str]:
    """``{item guid: art number}`` for the kinds the editor offers."""
    sys.path[:0] = [str(CAMPAIGN_ROOT),
                    str(ROOT / "campaign_editor")]
    import catalog
    art = {}
    for record in catalog.records("InventoryItemData"):
        if str(record.get("_t", "")).rsplit(".", 1)[-1] in KINDS:
            number = str(record.get("m_ArtNumber") or "")
            if re.fullmatch(r"a\d{7}", number):
                art[catalog._guid(record.get("m_Id"))] = number
    return art


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-root", type=Path, default=DEFAULT_CLIENT)
    parser.add_argument("--size", type=int, default=96)
    args = parser.parse_args()
    art = item_art()
    wanted = set(art.values())
    env = UnityPy.load(str(args.client_root / "AssetBundles" / "general" / "icons.ab"))
    OUT.mkdir(parents=True, exist_ok=True)
    saved = 0
    for obj in env.objects:
        if obj.type.name != "Texture2D":
            continue
        texture = obj.read()
        if texture.m_Name not in wanted or (OUT / f"{texture.m_Name}.png").exists():
            continue
        image = texture.image.convert("RGBA")
        if image.getchannel("A").getextrema()[1] < 200:
            image = image.convert("RGB")      # alpha unused by this texture (would read as blank)
        image.thumbnail((args.size, args.size))
        image.save(OUT / f"{texture.m_Name}.png")
        saved += 1
    present = {path.stem for path in OUT.glob("a*.png")}
    index = {guid: number for guid, number in art.items() if number in present}
    (OUT / "index.json").write_text(json.dumps(index, indent=0), encoding="utf-8")
    print(f"{saved} new icons; {len(index)}/{len(art)} items have art")


if __name__ == "__main__":
    main()
