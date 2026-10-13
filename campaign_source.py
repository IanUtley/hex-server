"""Resolve the campaign source tree kept at the project root (``campaign/``)."""

from pathlib import Path
import sys


CAMPAIGN_ROOT = Path(__file__).resolve().parent / "campaign"


def ensure_campaign_path() -> Path:
    """Make the campaign modules importable and return their root."""
    if not CAMPAIGN_ROOT.is_dir():
        raise RuntimeError(
            f"Campaign sources are missing at {CAMPAIGN_ROOT}.")
    path = str(CAMPAIGN_ROOT)
    if path not in sys.path:
        sys.path.insert(0, path)
    return CAMPAIGN_ROOT
