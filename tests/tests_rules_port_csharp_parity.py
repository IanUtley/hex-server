"""Execute original C# rules primitives against the Python semantic port."""

import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tests.test_db import fresh_database

fresh_database()   # bind this process's database before ``db`` is imported

from rules_port import (MultiplyWithCarryRng, permitted_next_phases,
                        validate_effect_coverage)
from rules_port.combat import CombatId, CombatManager
from rules_port import AbilityRegistry, Chain


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIENT_RNG = os.path.join(ROOT, "HexClient", "Assembly-CSharp-firstpass",
                          "MultiplyWithCarryRng.cs")
PROBE = os.path.join(os.path.dirname(__file__), "csharp_rules_rng_probe.cs")
COMBAT_PROBE = os.path.join(os.path.dirname(__file__), "csharp_rules_combat_probe.cs")
CHAIN_PROBE = os.path.join(os.path.dirname(__file__), "csharp_rules_chain_probe.cs")
MECHANICS = os.path.join(ROOT, "HexClient", "Assembly-CSharp-firstpass", "Game",
                           "Shared", "Mechanics")
CLIENT_COMBAT = os.path.join(MECHANICS, "Combat.cs")
CLIENT_CHAIN = os.path.join(MECHANICS, "Chain.cs")


def _csharp_rng(seed_z, seed_w, count):
    mcs = shutil.which("mcs")
    mono = shutil.which("mono")
    if not (mcs and mono):
        raise RuntimeError("C# parity requires mcs and mono")
    with tempfile.TemporaryDirectory(prefix="hex-rules-rng-") as work:
        binary = os.path.join(work, "probe.exe")
        subprocess.run([mcs, "-nologo", "-out:" + binary, CLIENT_RNG, PROBE],
                       check=True, cwd=ROOT, capture_output=True, text=True)
        output = subprocess.run(
            [mono, binary, str(seed_z), str(seed_w), str(count)],
            check=True, cwd=ROOT, capture_output=True, text=True).stdout
    return [int(line) for line in output.splitlines() if line.strip()]


def test_original_csharp_rng_matches_python_port_for_multiple_seeds():
    for seed_z, seed_w, count in ((12345, 67890, 5), (1, 2, 8),
                                  (0xFFFFFFFFFFFFFFFF, 9, 6)):
        original = _csharp_rng(seed_z, seed_w, count)
        port = MultiplyWithCarryRng(seed_z, seed_w)
        assert [port.next() for _ in range(count)] == original


def test_metadata_random_helpers_consume_the_same_session_rng():
    from abilities.framework.resolution import _choice, _randint, _sample
    state = {"_rules_rng": MultiplyWithCarryRng(12345, 67890)}
    expected = MultiplyWithCarryRng(12345, 67890)
    assert _randint(state, 2, 8) == expected.next_range(2, 9)
    values = [10, 20, 30, 40]
    assert _choice(state, values) == values[expected.next(len(values))]
    expected_sample = []
    remaining = list(values)
    for _ in range(2):
        index = expected.next(len(remaining))
        expected_sample.append(remaining.pop(index))
    assert _sample(state, values, 2) == expected_sample


def test_extracted_effect_inventory_has_no_unhandled_concrete_templates():
    assert validate_effect_coverage() == ((), ())


def test_python_phase_graph_matches_every_client_state_permit():
    source_states = {}
    for name in os.listdir(MECHANICS):
        if not name.endswith("State.cs"):
            continue
        with open(os.path.join(MECHANICS, name), encoding="utf-8-sig") as source:
            text = source.read()
        configured = re.search(r"Configure\(ETurnPhases\.(\w+)\)", text)
        if not configured:
            continue
        phase = configured.group(1)
        permits = frozenset(re.findall(r"\.Permit\(ETurnPhases\.(\w+)", text))
        if permits:
            source_states[phase] = permits
    assert source_states, "client state source was not parsed"
    for phase, permits in source_states.items():
        assert permitted_next_phases(phase) == permits, phase


def test_original_csharp_combat_declaration_matches_python_port():
    mcs = shutil.which("mcs")
    mono = shutil.which("mono")
    if not (mcs and mono):
        raise RuntimeError("C# parity requires mcs and mono")
    with tempfile.TemporaryDirectory(prefix="hex-rules-combat-") as work:
        binary = os.path.join(work, "probe.exe")
        subprocess.run([mcs, "-nologo", "-out:" + binary, CLIENT_COMBAT,
                        COMBAT_PROBE], check=True, cwd=ROOT,
                       capture_output=True, text=True)
        original = subprocess.run([mono, binary], check=True, cwd=ROOT,
                                  capture_output=True, text=True).stdout.strip()

    class Card:
        def __init__(self, uid):
            self.session_card_id = uid

    attacker, first, second, defender = Card(10), Card(20), Card(30), Card(99)
    combat = CombatManager().create_attack(CombatId(), 1, defender)
    combat.declare_attacker(attacker)
    combat.declare_blockers((first, second))
    valid = combat.assign_damage_order((30, 20))
    actual = ",".join((str(int(combat.flags)), str(valid),
                       str(combat.blockers[0].session_card_id),
                       str(combat.blockers[1].session_card_id)))
    assert actual == original


def test_original_csharp_chain_stack_identity_matches_python_port():
    mcs = shutil.which("mcs")
    mono = shutil.which("mono")
    if not (mcs and mono):
        raise RuntimeError("C# parity requires mcs and mono")
    with tempfile.TemporaryDirectory(prefix="hex-rules-chain-") as work:
        binary = os.path.join(work, "probe.exe")
        subprocess.run([mcs, "-nologo", "-out:" + binary, CLIENT_CHAIN,
                        CHAIN_PROBE], check=True, cwd=ROOT,
                       capture_output=True, text=True)
        original = subprocess.run([mono, binary], check=True, cwd=ROOT,
                                  capture_output=True, text=True).stdout.strip()

    class Ability:
        def __init__(self, instance_id):
            self.instance_id = instance_id

    registry = AbilityRegistry()
    first, second = Ability(7), Ability(9)
    chain = Chain(registry)
    chain.push_ability(first)
    chain.push_ability(second)
    wrong_pop = chain.pop_ability(7) is None
    right_pop = chain.pop_ability(9) is second
    actual = ",".join((str(chain.count), str(chain.peek_ability().instance_id),
                       str(wrong_pop), str(right_pop)))
    assert actual == original


if __name__ == "__main__":
    test_original_csharp_rng_matches_python_port_for_multiple_seeds()
    test_metadata_random_helpers_consume_the_same_session_rng()
    test_extracted_effect_inventory_has_no_unhandled_concrete_templates()
    test_python_phase_graph_matches_every_client_state_permit()
    test_original_csharp_combat_declaration_matches_python_port()
    test_original_csharp_chain_stack_identity_matches_python_port()
    print("PASS C# rules parity")
