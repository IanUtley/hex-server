"""Write one reveal plus activation-picker event batch for the C# client parser."""

from pathlib import Path
import struct
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from domain.enums import ECardUsage  # noqa: E402
from domain.events import (  # noqa: E402
    AbilityActivationDataRequiredSessionEventArgs,
    CardsRevealedSessionEventArgs,
    OptionInstanceSessionEventArgs,
    PlayerOptionListSessionEventArgs,
    PlayerOptionSessionEventArgs,
    TargetInstanceSessionEventArgs,
)
from domain.types import ResourceId, SessionCardId, UID  # noqa: E402


def build_reveal_picker_batch():
    session_id = UID(42)
    player_id = UID.make(244, 1234)
    source_card = SessionCardId(UID.make(1, 101))
    legal_target = SessionCardId(UID.make(1, 77))
    revealed_target = SessionCardId(UID.make(1, 78))
    unrelated_card = SessionCardId(UID.make(1, 102))

    reveal = CardsRevealedSessionEventArgs()
    reveal.session_id = session_id
    reveal.player_id = player_id
    reveal.session_card_ids = [legal_target, revealed_target]
    reveal.collections = [1, 1]
    reveal.owning_players = [player_id, player_id]
    reveal.positions = [0, 1]
    reveal.ability_instance_id = 700
    reveal.inactive = True

    activation = AbilityActivationDataRequiredSessionEventArgs()
    activation.session_id = session_id
    activation.player_id = player_id
    activation.ability_instance_id = 701
    activation.ability_parent_id = reveal.ability_instance_id
    activation.source_card_id = source_card
    activation.effect_instance_ids = [3]
    activation.resolve_chain = True

    def card_option(card, target_uid):
        target = TargetInstanceSessionEventArgs()
        target.session_id = session_id
        target.target_index = 2
        target.target_id = ResourceId.from_str(
            "5badd4e4-5af4-409d-9d1f-1b839b39c35e")
        target.targets = [target_uid]

        instance = OptionInstanceSessionEventArgs()
        instance.session_id = session_id
        instance.opt_id = ResourceId.from_str(
            "4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd")
        instance.target_ids = [target.target_id]
        instance.target_instances = [target]
        instance.min_target_counts = [1]
        instance.max_target_counts = [1]

        option = PlayerOptionSessionEventArgs()
        option.session_id = session_id
        option.card = card
        option.instances = [instance]
        option.state = ECardUsage.Activate
        return option

    options = PlayerOptionListSessionEventArgs()
    options.session_id = session_id
    options.player_id = player_id
    options.options = [
        card_option(source_card, legal_target),
        card_option(unrelated_card, legal_target),
    ]

    return [reveal, activation, options]


def write_fixture(path):
    events = build_reveal_picker_batch()
    payload = bytearray(struct.pack("<I", len(events)))
    for event in events:
        body = event.to_byte_array()
        payload.extend(struct.pack("<II", event.CLASS_ID, len(body)))
        payload.extend(body)
    Path(path).write_bytes(payload)


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_reveal_picker_fixture.py OUTPUT")
    write_fixture(sys.argv[1])


if __name__ == "__main__":
    main()
