"""Resolve the campaign source tree kept in the private submodule."""

from pathlib import Path
import sys


CAMPAIGN_ROOT = Path(__file__).resolve().parent / "hex-private" / "campaign"


def ensure_campaign_path() -> Path:
    """Make the private campaign modules importable and return their root."""
    if not CAMPAIGN_ROOT.is_dir():
        raise RuntimeError(
            "Campaign sources are missing; run "
            "`git submodule update --init hex-private`.")
    path = str(CAMPAIGN_ROOT)
    if path not in sys.path:
        sys.path.insert(0, path)
    return CAMPAIGN_ROOT
