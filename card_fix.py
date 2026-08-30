"""Post-extraction data fixes for card abilities.

Apply fixes to the DB after card templates and ability metadata have been
seeded from gamedata.  Each fix is a named function with a comment explaining
WHY the data value is overridden.

Called from static.ensure_schema() after all seeds are inserted.
"""

import json
import re


def apply_fixes(db):
    """Run all card-ability data fixes against the given DB connection.

    Fixes are idempotent — they check the current state before mutating.
    Call this after static.ensure_schema() has seeded the tables.
    """
    # ── Fixes ──────────────────────────────────────────────────────────
    # Order: add new fixes at the bottom.  Each fix has its own commit.
    fix_ancestors_chosen_ignore_chain(db)          # 2026-08-09: TurnStartedEvent triggers on chain unnecessarily
    fix_all_turnstarted_ignore_chain(db)           # 2026-08-09: all TurnStartedEvent triggers should resolve immediately (bulk follow-on)

    db.commit()


# ═══════════════════════════════════════════════════════════════════════
#  Individual fixes
# ═══════════════════════════════════════════════════════════════════════

# ── The Ancestors' Chosen ──────────────────────────────────────────────
# The card's TurnStartedEvent trigger (create Ancestral Specters into
# deck) has m_IgnoresChain=0 in gamedata, which pushes it onto the chain
# at every turn start.  The player sees a "hidden resolve" flash on the
# stack and disappear — confusing and meaningless since no player can
# respond to a blind deck-insert.  Override to resolve immediately.
_ANCESTORS_CHOSEN_ABILITIES = [
    # GUID 120a831d = "At the start of your turn, create two Ancestral Specters ..."
    "120a831d-90d6-c5e6-a789-b7e816978ccd",
    # GUID fefc3bae = "At the start of your turn, create three Ancestral Specters ..."
    "fefc3bae-9b5d-afaa-c094-9ecdf86904a1",
]


def fix_ancestors_chosen_ignore_chain(db):
    """Set m_IgnoresChain=1 for The Ancestors' Chosen TurnStartedEvent ability."""
    for guid in _ANCESTORS_CHOSEN_ABILITIES:
        _set_raw_json_ignores_chain(db, guid, 1)


# ── All TurnStartedEvent triggers ──────────────────────────────────────
# 272 abilities have TurnStartedEvent + m_IgnoresChain=0.  Turn-start
# triggers are automatic — they fire without player action and the
# opponent can never respond to them in the same phase (StartTurn is
# always auto-passed).  Putting them on the chain is a pure visual
# glitch.  Override all of them to resolve immediately.
def fix_all_turnstarted_ignore_chain(db):
    """Set m_IgnoresChain=1 for every TurnStartedEvent trigger still at 0."""
    rows = db.execute(
        "SELECT ability_guid FROM card_abilities_meta "
        "WHERE trigger_event_type='Game.Shared.Mechanics.TurnStartedEvent' "
        "AND raw_json LIKE '%\"m_IgnoresChain\": 0%'"
    ).fetchall()
    for (guid,) in rows:
        _set_raw_json_ignores_chain(db, guid, 1)


# ═══════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════

def _set_raw_json_ignores_chain(db, ability_guid, value):
    """Update a single ability's raw_json: set m_IgnoresChain to *value* (0 or 1)."""
    row = db.execute(
        "SELECT raw_json FROM card_abilities_meta WHERE ability_guid=?",
        (ability_guid,),
    ).fetchone()
    if not row:
        return
    raw = row[0]
    new_raw = re.sub(
        r'"m_IgnoresChain":\s*\d+',
        f'"m_IgnoresChain": {value}',
        raw,
    )
    if new_raw != raw:
        db.execute(
            "UPDATE card_abilities_meta SET raw_json=? WHERE ability_guid=?",
            (new_raw, ability_guid),
        )
