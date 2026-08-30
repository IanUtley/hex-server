#!/usr/bin/env python3
"""Command-line entry point for the environment-driven gamedata extractor.

Examples::

    HEX_GAMEDATA=/path/to/Data/gamedata python3 AssetExtraction/extract_gamedata.py
    HEX_GAMEDATA=/path/to/Data/gamedata python3 AssetExtraction/extract_gamedata.py \
        --compare-db hconnect.db
    HEX_GAMEDATA=/path/to/Data/gamedata python3 AssetExtraction/extract_gamedata.py \
        --manifest /tmp/hex-gamedata.json

The actual parser and database writer live in ``gamedata_seed.py`` so server
startup and offline comparisons use exactly the same code path.
"""

from gamedata_seed import main


if __name__ == "__main__":
    main()
