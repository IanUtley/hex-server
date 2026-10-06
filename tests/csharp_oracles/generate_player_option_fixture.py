"""Write a representative Python PlayerOptionList event for the C# reader."""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from domain.events import (  # noqa: E402
    CostInstanceSessionEventArgs,
    OptionInstanceSessionEventArgs,
    PlayerOptionListSessionEventArgs,
    PlayerOptionSessionEventArgs,
    TargetInstanceSessionEventArgs,
)
from domain.enums import ECardUsage  # noqa: E402
from domain.types import ResourceId, SessionCardId, UID  # noqa: E402


def build_player_option_event():
    session_id = UID(42)
    player_id = UID.make(244, 1234)
    card_id = SessionCardId(UID.make(1, 101))
    target_id = ResourceId.from_str(
        "5badd4e4-5af4-409d-9d1f-1b839b39c35e")
    cost_id = ResourceId.from_str("00000000-0000-0000-0000-000000000123")

    target = TargetInstanceSessionEventArgs()
    target.session_id = session_id
    target.target_index = 2
    target.target_id = target_id
    target.targets = [SessionCardId(UID.make(1, 77))]
    target.additional_targets = [SessionCardId(UID.make(1, 78))]

    cost = CostInstanceSessionEventArgs()
    cost.session_id = session_id
    cost.min_target_count = 1
    cost.max_target_count = 1
    cost.cost_type = 2  # EAbilityCostType.SacrificeAbilityCostType
    cost.targets = [SessionCardId(UID.make(1, 99))]
    cost.target_template_id = cost_id

    option = OptionInstanceSessionEventArgs()
    option.session_id = session_id
    option.opt_id = ResourceId.from_str(
        "4ef824d5-5d8f-4b4f-9e77-63f82c6b17cd")
    option.target_ids = [target_id, cost_id]
    option.target_instances = [target, cost]
    option.min_target_counts = [1, 1]
    option.max_target_counts = [1, 1]

    card_option = PlayerOptionSessionEventArgs()
    card_option.session_id = session_id
    card_option.card = card_id
    card_option.instances = [option]
    card_option.state = ECardUsage.Activate

    event = PlayerOptionListSessionEventArgs()
    event.session_id = session_id
    event.player_id = player_id
    event.options = [card_option]
    return event


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_player_option_fixture.py OUTPUT")
    Path(sys.argv[1]).write_bytes(build_player_option_event().to_byte_array())


if __name__ == "__main__":
    main()
