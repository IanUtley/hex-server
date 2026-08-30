# services/ TODO

## Chat
- [ ] **Fix cross-client chat broadcast** — two clients in the same room
      (e.g. `general`) do not see each other's messages, though both receive
      their own echo. Broadcast loop in `services/chat.py` iterates
      `_active_clients` and calls `h.send()`, but nothing reaches the other
      client. Root-cause hypotheses (in priority order):
      - `scnt` race: each connection runs in its own daemon thread
        (`hconnect_server.py` `threading.Thread(target=handler.handle)`), and
        the broadcast mutates `h.scnt += 1` + `h.send()` on the *other*
        handler's object while that handler's own thread also mutates `scnt`
        — no lock around `scnt`/`sendall`. Client rejects out-of-order `scnt`
        so the other client never processes the message. Fix: per-handler
        send lock serializing `scnt += 1` + `conn.sendall`.
      - Broadcast `issuer`/headers may be missing fields the chat handler
        expects (e.g. `ccnt`, `reqid`) — compare with what the sender's own
        echo uses.
      - Verify the client actually renders a pushed `rchat` (not just the
        response to its own send) — confirm with a second account (God#0552
        and Devil#5805 both auth and join `general`; "Broadcast to N other
        users" was never logged, so debug logging added to the loop).
