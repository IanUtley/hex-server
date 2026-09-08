"""Polling worker for replay artifact generation.

The format/database work lives in :mod:`replay`; this module is only the
long-running process wrapper used by ``restart.sh`` and Docker.
"""

import time

from replay import DB_PATH, POLL_SECONDS, REPLAY_DIR, process_once


def run():
    print(f"[replay_server] Watching {DB_PATH}; output={REPLAY_DIR}", flush=True)
    while True:
        try:
            built = process_once()
            if built:
                print(f"[replay_server] Built {built} replay(s)", flush=True)
        except Exception as exc:
            print(f"[replay_server] Worker error: {exc}", flush=True)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        pass
