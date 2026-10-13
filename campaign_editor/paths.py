"""Where the campaign editor finds the project, the campaign sources and Records.

The editor lives at the project root (``campaign_editor/``), and so does the
campaign it edits (``campaign/``):

* ``REPO``          - the project root (holds ``hconnect.db`` and the server modules).
* ``CAMPAIGN_ROOT`` - ``HEX_CAMPAIGN_ROOT`` if set, else ``REPO/campaign``
                      (``campaign_rules/``, ``campaign_data/``, ``campaign_runtime/``).
* ``RECORDS``       - ``HEX_RECORDS`` if set, else ``REPO/hex-private/Records``
                      (the same variable the server bootstrap reads).
* ``GENERATED``     - ``campaign_editor/generated`` (art extracted from the client).

Importing this module puts ``REPO`` and ``CAMPAIGN_ROOT`` on ``sys.path`` so
``campaign_rules``, ``campaign_runtime.*`` and the server modules import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(os.path.expanduser(value)).resolve() if value else default


HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CAMPAIGN_ROOT = _env_path("HEX_CAMPAIGN_ROOT", REPO / "campaign")
RECORDS = _env_path("HEX_RECORDS", REPO / "hex-private" / "Records")
GENERATED = HERE / "generated"

for _root in (REPO, CAMPAIGN_ROOT):
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
