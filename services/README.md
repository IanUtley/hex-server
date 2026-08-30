# services/

Service handler dispatch table for the HConnect protocol.

## How it works

The `_SERVICE_TABLE` dictionary maps `data_type` integers to
fully-qualified module paths. It is the service ownership registry and the
target shape for the extracted service architecture.

The legacy server still performs the main dispatch in
`HCPHandler.handle_service_request()` and its compatibility helpers. Several
service modules are already extracted, but adding a table entry alone does not
automatically route traffic until the live dispatcher calls `services.dispatch`.

## Adding a new service handler

1. Create a handler module under `services/`, e.g. `services/tournament.py`:

```python
def handle_create_tournament(dt, handler, target, instance, reqid,
                              comp, session_id, conh, inner_obj, inner_bytes, log_req):
    ...
```

2. Add an entry to `_SERVICE_TABLE` in `services/__init__.py`:

```python
_SERVICE_TABLE = {
    ...
    99999: "services.tournament.handle_create_tournament",
}
```

Only a single-line dict entry is needed — trivial to merge when two
contributors add different services.

## Current dispatch table

The registry currently covers the main families:
- **Mail** (60002–60007)
- **Profile** (2043, 2081–2095, 2127, 2185–2187)
- **Store / Escrow** (6009, 6011, 6013)
- **LoadBalancer** (22013–22031)
- **GameSession** (3027)
- **Campaign** (110000, 150000)
- **Battle** (3029)
- **Arena** (10001–10013)

Check `_SERVICE_TABLE` and the live handler before documenting a new data type;
some protocol numbers are aliases or fire-and-forget requests rather than
ordinary request/response handlers.
