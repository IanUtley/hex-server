"""Moved to ``campaign_runtime/offers.py`` (shared with the server's campaign
runtime); this name stays importable for the editor's modules."""

import sys

import paths  # noqa: F401  (puts campaign on sys.path)
from campaign_runtime import offers as _offers  # noqa: E402

sys.modules[__name__] = _offers
