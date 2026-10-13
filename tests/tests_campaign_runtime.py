"""The campaign runtime plays the starter run from the editor's rules."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
CAMPAIGN_ROOT = os.path.join(ROOT, "hex-private", "campaign")
sys.path[:0] = [ROOT, CAMPAIGN_ROOT,
                os.path.join(ROOT, "campaign_editor")]

import server_playthrough  # noqa: E402


def test_starter_run_reaches_az1_through_the_server_runtime():
    # AZ0 panorama -> training battle (lost once, retried) -> Crayburn ->
    # report -> AZ1, through campaign.py's protocol handlers on a DB copy.
    ok, route = server_playthrough.run("Dwarf", set(), lose_first=True)
    assert ok, "\n".join(route)
    assert any("LOST" in step for step in route), route
    assert any("az01_q_dwarf_crayburn: objective 2 finished" in step
               for step in route), route


if __name__ == "__main__":
    test_starter_run_reaches_az1_through_the_server_runtime()
    print("PASS test_starter_run_reaches_az1_through_the_server_runtime")
