"""Container health check for the public Hex service listeners."""

from __future__ import annotations

import sys


PORTS = {
    "hconnect": 9933,
    "proxy": 8081,
}
LISTENING_STATE = "0A"


def listening_ports() -> set[int]:
    """Return TCP ports with a LISTEN socket in this container."""
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="ascii") as stream:
                next(stream, None)  # column headings
                for line in stream:
                    fields = line.split()
                    if len(fields) < 4 or fields[3] != LISTENING_STATE:
                        continue
                    try:
                        ports.add(int(fields[1].rsplit(":", 1)[1], 16))
                    except (IndexError, ValueError):
                        continue
        except OSError:
            continue
    return ports


def main() -> int:
    active_ports = listening_ports()
    failures = []
    for name, port in PORTS.items():
        if port not in active_ports:
            failures.append(f"{name}:{port} (no listening socket)")

    if failures:
        print("unhealthy: " + "; ".join(failures), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
