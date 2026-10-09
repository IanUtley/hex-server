"""Optional timing log for the rules-port transaction path."""

import os
import time

from db import log_req


def _rules_port_timing(label, started, **fields):
    """Emit opt-in timing evidence for a synchronous RulesPort boundary."""
    if not os.environ.get("HEX_RULES_PORT_TIMING"):
        return
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    suffix = " ".join(f"{key}={value!r}" for key, value in fields.items())
    log_req(f"    RulesPort timing {label}: {elapsed_ms:.1f}ms"
            + (f" {suffix}" if suffix else ""))
