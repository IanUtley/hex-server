"""Helpers for reading one isolated Unity headless coverage run."""

import os
import re


RUN_BEGIN = re.compile(r"\[HexCoverageRun\] begin id=([^ ]+)")


def scoped_client_lines(path):
    """Return lines for the requested/latest probe run.

    Older logs predate run markers, so the complete file remains the fallback.
    HEX_HEADLESS_RUN_ID can select a specific marker when a log contains more
    than one run that must be compared deliberately.
    """
    with open(path, errors="replace") as stream:
        lines = stream.readlines()
    markers = [(index, match.group(1))
               for index, line in enumerate(lines)
               for match in [RUN_BEGIN.search(line)] if match]
    if not markers:
        return lines
    requested = os.environ.get("HEX_HEADLESS_RUN_ID", "").strip()
    if requested and requested != "latest":
        selected = [item for item in markers if item[1] == requested]
        if not selected:
            raise RuntimeError("HEX_HEADLESS_RUN_ID not found: " + requested)
        start = selected[-1][0]
    else:
        start = markers[-1][0]
    return lines[start:]
