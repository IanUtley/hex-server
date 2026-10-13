"""An answered invoked discard picker must release the port activation slot.

Live symptom: the AI's Giant Corpse Fly Deploy ("target champion chooses and
discards a card", an ActivateAbility invoking DiscardACard) paused for the
player's discard.  The slot ``pending_activation`` named the suspended Deploy
PARENT, while the release compared it only with the child continuation, so it
was never cleared.  On the player's next turn ``_push_main_phase_options``
saw a pending activation, returned without sending options, and the client
showed only "Continue to Combat" (no shard could be played).
"""

import os
import sys
import types
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def test_parent_named_activation_slot_released_after_child_resumes():
    from rules_port import resolution
    from rules_port.actions import AbilityResolutionState

    deploy = "566e2eda-a7ba-a62c-cd99-2bd77573b701"
    discard = "06570445-27e3-fc87-2e17-a7b5e1de693d"
    parent = {"ability_instance_id": 12, "ability_guid": deploy,
              "source_uid": 16385, "owner_id": 0, "target_map": {"0": [257]},
              "variables": {}, "resume_effect_order": 1}
    child = {"ability_instance_id": 13, "instance_id": 13,
             "ability_guid": discard, "source_uid": 16385, "owner_id": 0,
             "resume_effect_order": 0, "parent": parent}
    port = types.SimpleNamespace(
        pending_activation={"ability_instance_id": 12,
                            "responsible_player_id": 0,
                            "continuation": dict(parent), "prompts": ()},
        persist=mock.Mock())
    session = types.SimpleNamespace(_rules_port_session=port)
    resumed = []

    def fake_resolve(*args, **kwargs):
        resumed.append(args[7])
        return AbilityResolutionState.COMPLETED

    with mock.patch.object(resolution, "resolve_port_ability", fake_resolve):
        resolution.resume_ability_continuation_parents(
            None, None, session, None, None, None, {}, child)

    assert resumed == [deploy]
    assert port.pending_activation is None
    port.persist.assert_called()


def test_unrelated_activation_slot_is_kept():
    from rules_port import resolution
    from rules_port.actions import AbilityResolutionState

    child = {"ability_instance_id": 13, "ability_guid": "x",
             "parent": {"ability_instance_id": 12, "ability_guid": "y"}}
    slot = {"ability_instance_id": 40, "continuation": {}, "prompts": ()}
    port = types.SimpleNamespace(pending_activation=slot, persist=mock.Mock())
    session = types.SimpleNamespace(_rules_port_session=port)
    with mock.patch.object(resolution, "resolve_port_ability",
                           lambda *a, **k: AbilityResolutionState.COMPLETED):
        resolution.resume_ability_continuation_parents(
            None, None, session, None, None, None, {}, child)
    assert port.pending_activation is slot
