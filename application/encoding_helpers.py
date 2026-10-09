"""ObjFmt encoders for inventory, card-instance, deck and champion bit fields."""

import encoder


def encode_inventory_item(buf, sizes, ft, template_guid, item_id, elem_idx, quantity=1, bound=True):
    import hconnect_server as _hc
    return _hc.encoder.encode_inventory_item(buf, sizes, ft, template_guid, item_id, elem_idx, quantity, bound)


def encode_card_instance(buf, sizes, ft, guid, name, card_id, cost, atk, def_, idx):
    import hconnect_server as _hc
    return _hc.encoder.encode_card_instance(buf, sizes, ft, guid, name, card_id, cost, atk, def_, idx)


def encode_deck_bits(buf, sizes, ft, did, dname, did_val, champ_did, card_guids, idx):
    import hconnect_server as _hc
    return _hc.encoder.encode_deck_bits(buf, sizes, ft, did, dname, did_val, champ_did, card_guids, idx)


def encode_champion_bits_minimal(buf, sizes, ft, cu64, cname, cid, lvl, xp, cc, rc, gd, idx,
                                 last_campaign_id=0, last_deck_id=0, talents=None,
                                 pet_name=""):
    import hconnect_server as _hc
    return _hc.encoder.encode_champion_bits_minimal(buf, sizes, ft, cu64, cname, cid, lvl, xp, cc, rc, gd, idx,
                                                last_campaign_id, last_deck_id, talents, pet_name)
