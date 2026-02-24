"""
Data-driven UI scenario runner.

Discovers YAML scenario files in tests/ui/scenarios/ and runs each one as a
parametrized pytest case.  Each scenario is a goal description — the harness
uses Claude Vision to figure out the steps.

    uv run pytest tests/ui/ -v --instance ui-test
"""

from pathlib import Path

import pytest
import yaml

from .harness import UIHarness

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def discover_scenarios():
    """Yield (id, path) for every .yaml scenario file."""
    if not SCENARIOS_DIR.exists():
        return
    for f in sorted(SCENARIOS_DIR.rglob("*.yaml")):
        yield f.stem, f


@pytest.mark.parametrize(
    "scenario_path",
    [p for _, p in discover_scenarios()],
    ids=[name for name, _ in discover_scenarios()],
)
def test_scenario(scenario_path, base_url, page, screenshot_dir):
    scenario = yaml.safe_load(scenario_path.read_text())
    name = scenario_path.stem
    harness = UIHarness(page, base_url, screenshot_dir, name)

    result = harness.run(scenario["description"])

    assert result["status"] == "done", (
        f"Scenario failed: {result.get('reason', 'unknown')}\n"
        f"Steps taken: {result['steps']}"
    )
