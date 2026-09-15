"""Native creation-replacement state used by RulesPort card effects."""

from __future__ import annotations

import json
import re


def _replacement_abilities(db, ability_guids):
    from gamedata import DEFAULT_RECORD_STORE, ability_graph
    from pvp_db import db_template_exists
    result = []
    for guid in ability_guids or ():
        graph = ability_graph(DEFAULT_RECORD_STORE, str(guid).lower())
        if graph is None:
            continue
        for effect in graph.effects:
            template = effect.template.to_dict() if effect.template else {}
            modifier = template.get("m_Modifier") or {}
            attr = str(modifier.get("m_Attribute") or "")
            if "create" not in attr.lower() or "instead" not in attr.lower():
                continue
            linked = []
            def walk(value):
                if isinstance(value, dict):
                    ident = value.get("m_Guid")
                    if ident and db_template_exists(str(ident).lower(), conn=db):
                        linked.append(str(ident).lower())
                    for child in value.values():
                        walk(child)
                elif isinstance(value, list):
                    for child in value:
                        walk(child)
            walk(graph.source.to_dict())
            result.append((attr, tuple(dict.fromkeys(linked))))
    return result


def activate_creation_replacements(db, session_id, card_uid):
    from pvp_db import (db_card_grant_info, db_card_mutation_field,
                        db_card_template_ability_payload,
                        db_set_card_abilities, db_set_card_mutation_field)
    row = db_card_grant_info(session_id, int(card_uid), conn=db)
    if not row:
        return False
    try:
        current = [str(value).lower() for value in json.loads(row[0] or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError):
        current = []
    try:
        template = json.loads(db_card_template_ability_payload(
            row[1], conn=db) or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        template = []
    combined = list(dict.fromkeys(current + [str(value).lower() for value in template]))
    replacements = _replacement_abilities(db, combined)
    if not replacements:
        return False
    try:
        buffs = json.loads(db_card_mutation_field(
            session_id, int(card_uid), "permanent_buffs", conn=db) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        buffs = {}
    if not isinstance(buffs, dict):
        buffs = {}
    attrs = buffs.setdefault("int_attrs", {})
    changed = combined != current
    for attr, _linked in replacements:
        if int(attrs.get(attr, 0) or 0) != 1:
            attrs[attr] = 1
            changed = True
    if not changed:
        return False
    db_set_card_abilities(session_id, int(card_uid), json.dumps(combined), conn=db)
    db_set_card_mutation_field(session_id, int(card_uid), "permanent_buffs",
                               json.dumps(buffs, separators=(",", ":")), conn=db)
    db.commit()
    return True
