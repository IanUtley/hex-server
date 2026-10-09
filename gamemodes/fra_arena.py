"""FRA arena setup values and session state data."""

import game_engine


def _fra_arena_setup_values(encounter_data):
    """Normalize the encounter identity needed by the FRA client session."""
    import hconnect_server as _hc
    if not isinstance(encounter_data, dict):
        return None
    raw_flags = encounter_data.get("SessionFlags", 0)
    if isinstance(raw_flags, dict):
        raw_flags = raw_flags.get("value__", 0)
    try:
        flags = int(raw_flags or 0)
    except (TypeError, ValueError):
        return None
    arena_flag = int(_hc.game_engine.ESessionFlags.IsPvEArena)
    if not flags & arena_flag:
        return None

    def _as_uint64(field):
        value = encounter_data.get(field, 0)
        if isinstance(value, dict):
            value = value.get("value__", value.get("m_UID64", 0))
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    return {
        "flags": (flags | int(_hc.game_engine.ESessionFlags.IsPvE) | arena_flag),
        "arena_instance": _as_uint64("ArenaInstance"),
        "arena_owner": _as_uint64("ArenaOwner"),
    }


def _fra_arena_session_state_data(values, flags=None):
    """Build the FRA EncounterData object used in client setup responses.

    ``flags`` overrides the saved setup flags so the transient StartEncounter
    session can carry the arena identity without advertising IsPvE/IsPvEArena.
    """
    return ("struct", ("Game.Shared.SessionStateEncounterData", [
        ("SceneTemplateId", "struct", ("Game.Shared.ResourceId", [
            ("m_Guid", "guid", "00000000-0000-0000-0000-000000000000")])),
        ("SessionFlags", "enum1", (
            "Game.Shared.ESessionFlags",
            values["flags"] if flags is None else int(flags))),
        ("ArenaInstance", "ulong", values["arena_instance"]),
        ("ArenaOwner", "ulong", values["arena_owner"]),
    ]))
