"""A block carries its kind, instead of being recovered from its English.

doctor buckets a blocked recipe and the palette labels it; both used to match
substrings of the reason, so rewording "needs a dns name" quietly moved rows
between buckets. `check_recipe` names the kind and says whether the value is
missing (set it) or refused (look at what is there); the reason is only text.
"""

from __future__ import annotations

import pytest

from fieldlog.recipes import check_chain, check_recipe, is_blocked, load_catalog
from fieldlog.state import TargetSession
from fieldlog.tui.helpers import short_reason

TOOL = {"id": "true", "bin": "true"}
GHOST = {"id": "ghost", "bin": "definitely-not-a-real-binary-xyz"}

TARGET = {"id": "t", "flags": "$TARGET"}
HOST = {"id": "h", "flags": "$HOST"}
LHOST = {"id": "l", "flags": "-B $LHOST"}
IFACE = {"id": "i", "flags": "-I $IFACE"}


@pytest.fixture(autouse=True)
def no_interface_address(monkeypatch):
    """No interface on this machine has an address, so an unset $LHOST stays
    unset — otherwise the lhost cases depend on whose laptop runs the suite."""
    monkeypatch.setattr("fieldlog.state.get_interface_ip", lambda _name: "")


CASES = [
    ("target unset",   TOOL,  TARGET, {},                          "target",    True,  "needs target"),
    ("target a flag",  TOOL,  TARGET, {"target": "-f"},            "target",    False, "bad target"),
    ("target a typo",  TOOL,  TARGET, {"target": "10.0.0.256"},    "target",    False, "bad target"),
    ("target unsafe",  TOOL,  TARGET, {"target": "a;b"},           "target",    False, "bad target"),
    ("dns unset",      TOOL,  HOST,   {},                          "dns",       True,  "needs dns name"),
    ("dns a flag",     TOOL,  HOST,   {"hostname": "-x"},          "dns",       False, "bad dns name"),
    ("lhost unset",    TOOL,  LHOST,  {"interface": "nosuch0"},    "lhost",     True,  "needs lhost"),
    ("lhost a flag",   TOOL,  LHOST,  {"lhost": "-l"},             "lhost",     False, "bad lhost"),
    ("iface unsafe",   TOOL,  IFACE,  {"interface": "a;b"},        "interface", False, "bad interface"),
    ("no binary",      GHOST, TARGET, {"target": "10.0.0.1"},      "binary",    False, "not installed"),
]


@pytest.mark.parametrize(
    ("tool", "preset", "scope", "kind", "missing"),
    [pytest.param(*c[1:6], id=c[0]) for c in CASES],
)
def test_a_block_knows_its_kind_and_whether_the_value_is_missing(
    tool: dict, preset: dict, scope: dict, kind: str, missing: bool
):
    verdict = check_recipe(tool, preset, TargetSession(**scope))
    assert verdict.blocked is True
    assert (verdict.kind, verdict.missing) == (kind, missing)


@pytest.mark.parametrize(
    ("tool", "preset", "scope", "label"),
    [pytest.param(c[1], c[2], c[3], c[6], id=c[0]) for c in CASES],
)
def test_the_palette_label_follows_the_kind(tool: dict, preset: dict, scope: dict, label: str):
    assert short_reason(check_recipe(tool, preset, TargetSession(**scope))) == label


def test_a_recipe_that_can_run_is_ready():
    verdict = check_recipe(TOOL, TARGET, TargetSession(target="10.0.0.1"))
    assert verdict.blocked is False
    assert verdict.kind == "ready"
    assert verdict.missing is False


@pytest.mark.parametrize(
    ("tool", "preset", "scope"), [pytest.param(*c[1:4], id=c[0]) for c in CASES]
)
def test_is_blocked_still_returns_the_pair_its_callers_read(tool: dict, preset: dict, scope: dict):
    session = TargetSession(**scope)
    verdict = check_recipe(tool, preset, session)
    assert is_blocked(tool, preset, session) == (verdict.blocked, verdict.reason)


def _chain_catalog(tmp_path):
    """One chain whose first step needs a target, and nothing else on disk."""
    base = tmp_path / "base.yaml"
    base.write_text(
        "recipes:\n"
        "  - id: 'true'\n"
        "    bin: true\n"
        "    presets:\n"
        "      - id: t\n"
        "        flags: $TARGET\n"
        "      - id: plain\n"
        "        flags: ''\n"
        "chains:\n"
        "  - id: c\n"
        "    steps: ['true/t', 'true/plain']\n",
        encoding="utf-8",
    )
    dropins = tmp_path / "recipes.d"
    dropins.mkdir(exist_ok=True)
    return load_catalog(base=base, dropin_dir=dropins)


def test_a_chain_carries_the_kind_of_the_step_that_blocks_it(tmp_path):
    cat = _chain_catalog(tmp_path)
    verdict = check_chain(cat, cat.chains[0], TargetSession())
    assert (verdict.kind, verdict.missing) == ("target", True)
    assert verdict.reason.startswith("step 1 true/t: ")
    # A chain that blocks on a missing target buckets as the recipe would.
    assert short_reason(verdict) == "needs target"


def test_a_chain_with_nothing_in_its_way_is_ready(tmp_path):
    cat = _chain_catalog(tmp_path)
    verdict = check_chain(cat, cat.chains[0], TargetSession(target="10.0.0.1"))
    assert (verdict.blocked, verdict.kind, verdict.reason) == (False, "ready", "2 steps ready")
