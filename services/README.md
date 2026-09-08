# services/

The service modules implement client-request families. The authoritative
request registry is `_SERVICE_TABLE` in `services/__init__.py`; the live
HConnect dispatcher consults it before falling back to the legacy compatibility
handler for requests that have not yet been extracted.

Each entry maps a client `data_type` to `(module, function, extra_kwargs)`. A
handler receives the normalized request context from `hconnect_server.py` and
owns its response/domain behavior. Add new extracted requests to the table and
include a focused protocol test where practical.

Auction remains registered as an intentionally unimplemented API surface.
Practice does not have a separate service module: it is a single client action
handled by the main session path. Replay packaging lives in the root
`replay.py`; `replay_server.py` is only its polling worker.
