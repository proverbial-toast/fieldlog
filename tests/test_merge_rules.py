"""One merge rule, inside a file and across files.

A repeated tool id adds its presets to the tool defined first; a repeated
`tool/preset` id replaces the earlier one; a `name:` or `bin:` written again on
a tool that already exists is ignored. Only the message differs — a repeat
across files is an expected override, the same repeat inside one file is a
mistake. Nothing is ever dropped over a message.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Dict, Optional

from fieldlog.recipes import Catalog, display_path, load_catalog


def _catalog(tmp_path: Path, dropins: Dict[str, str], base: str = "") -> Catalog:
    """A catalog from a base file and `{filename: yaml}`, nothing else scanned."""
    (tmp_path / "base.yaml").write_text(textwrap.dedent(base), encoding="utf-8")
    dropin_dir = tmp_path / "recipes.d"
    dropin_dir.mkdir(exist_ok=True)
    for name, text in dropins.items():
        (dropin_dir / name).write_text(textwrap.dedent(text), encoding="utf-8")
    return load_catalog(base=tmp_path / "base.yaml", dropin_dir=dropin_dir)


def _tool(cat: Catalog, tool_id: str) -> dict:
    return next(t for t in cat.tools if t["id"] == tool_id)


def _flags(cat: Catalog, tool_id: str, preset_id: str) -> str:
    return next(p for p in _tool(cat, tool_id)["presets"] if p["id"] == preset_id)["flags"]


def _one(messages, phrase: str) -> Optional[str]:
    """The single message carrying `phrase`, or None — never two for one mistake."""
    hits = [m for m in messages if phrase in m]
    assert len(hits) <= 1, hits
    return hits[0] if hits else None


PING = """
    recipes:
      - id: ping
        bin: true
        presets:
          - id: quick
            flags: "-c 1"
    """


def test_the_same_preset_twice_in_one_file_is_reported_and_the_last_kept(tmp_path: Path):
    cat = _catalog(tmp_path, {"a.yaml": """
        recipes:
          - id: ping
            bin: true
            presets:
              - id: quick
                flags: "-c 1"
          - id: ping
            presets:
              - id: quick
                flags: "-c 9"
        """})

    assert _flags(cat, "ping", "quick") == "-c 9"
    assert _one(cat.errors, "ping/quick defined twice") == (
        "recipes.d/a.yaml: ping/quick defined twice · last one kept"
    )
    # The mistake costs a message, never the tool.
    assert _tool(cat, "ping")["bin"] == "true"


def test_a_preset_repeated_inside_one_entry_is_the_same_mistake(tmp_path: Path):
    cat = _catalog(tmp_path, {"a.yaml": """
        recipes:
          - id: ping
            bin: true
            presets:
              - id: quick
                flags: "-c 1"
              - id: quick
                flags: "-c 9"
        """})

    assert _flags(cat, "ping", "quick") == "-c 9"
    assert _one(cat.errors, "ping/quick defined twice")


def test_a_dropin_cannot_give_an_existing_tool_a_new_bin(tmp_path: Path):
    cat = _catalog(tmp_path, {"a.yaml": """
        recipes:
          - id: ping
            bin: /opt/ping
            presets:
              - id: slow
                flags: "-c 9"
        """}, base=PING)

    assert _tool(cat, "ping")["bin"] == "true"
    message = _one(cat.errors, "ping: bin ignored")
    assert message and "set bin: on the preset" in message
    # The presets still land: the bin is refused, the file is not.
    assert [p["id"] for p in _tool(cat, "ping")["presets"]] == ["quick", "slow"]


def test_adding_a_preset_to_a_tool_whose_bin_is_not_its_id_says_nothing(tmp_path: Path):
    """The common drop-in: `web` runs python3, and a later file adds a preset to
    it without restating that. Nothing was overridden, so nothing is reported."""
    cat = _catalog(tmp_path, {
        "a.yaml": """
            recipes:
              - id: web
                bin: python3
                presets:
                  - id: serve
                    flags: "-m http.server"
            """,
        "b.yaml": """
            recipes:
              - id: web
                presets:
                  - id: dump
                    flags: "-c 'print(1)'"
            """,
    })

    assert [m for m in cat.errors if "web" in m] == []
    assert _tool(cat, "web")["bin"] == "python3"
    assert [p["id"] for p in _tool(cat, "web")["presets"]] == ["serve", "dump"]


def test_a_preset_may_carry_a_bin_of_its_own(tmp_path: Path):
    """The escape hatch the `bin ignored` message points at."""
    cat = _catalog(tmp_path, {"a.yaml": """
        recipes:
          - id: ping
            presets:
              - id: alt
                bin: /opt/ping
                flags: "-c 1"
        """}, base=PING)

    alt = next(p for p in _tool(cat, "ping")["presets"] if p["id"] == "alt")
    assert alt["bin"] == "/opt/ping"
    assert _tool(cat, "ping")["bin"] == "true"
    assert cat.errors == []


def test_across_files_a_repeat_is_an_override_not_an_error(tmp_path: Path):
    cat = _catalog(tmp_path, {
        "a.yaml": """
            recipes:
              - id: ping
                bin: true
                presets:
                  - id: quick
                    flags: "-c 1"
            """,
        "b.yaml": """
            recipes:
              - id: ping
                presets:
                  - id: quick
                    flags: "-c 9"
            """,
    })

    assert _flags(cat, "ping", "quick") == "-c 9"
    assert "ping/quick overridden by recipes.d/b.yaml" in cat.overrides
    assert [m for m in cat.errors if "ping/quick" in m] == []


def test_the_base_file_is_merged_by_the_same_rule(tmp_path: Path):
    cat = _catalog(tmp_path, {}, base="""
        recipes:
          - id: ping
            bin: true
            presets:
              - id: quick
                flags: "-c 1"
              - id: quick
                flags: "-c 9"
        """)

    assert _flags(cat, "ping", "quick") == "-c 9"
    message = _one(cat.errors, "ping/quick defined twice")
    assert message and message.startswith(display_path(tmp_path / "base.yaml") + ": ")


def test_a_later_name_does_not_rename_a_tool(tmp_path: Path):
    cat = _catalog(tmp_path, {"a.yaml": """
        recipes:
          - id: ping
            name: something else
            presets:
              - id: slow
                flags: "-c 9"
        """}, base="""
        recipes:
          - id: ping
            bin: true
            name: reachability
            presets:
              - id: quick
                flags: "-c 1"
        """)

    assert _tool(cat, "ping")["name"] == "reachability"
    message = _one(cat.errors, "ping: name ignored")
    assert message and message.startswith("recipes.d/a.yaml: ")


def test_restating_the_same_bin_and_name_is_not_a_change(tmp_path: Path):
    """The drop-in an operator writes by copying the base entry and adding to
    it restates `bin:` and `name:` word for word. Nothing was overridden, so
    every load must not say something was ignored."""
    cat = _catalog(tmp_path, {"a.yaml": """
        recipes:
          - id: ping
            bin: true
            name: reachability
            presets:
              - id: slow
                flags: "-c 9"
        """}, base="""
        recipes:
          - id: ping
            bin: true
            name: reachability
            presets:
              - id: quick
                flags: "-c 1"
        """)

    assert cat.errors == []
    assert [p["id"] for p in _tool(cat, "ping")["presets"]] == ["quick", "slow"]


def test_an_entry_that_only_restates_a_tool_adds_no_phantom_recipe(tmp_path: Path):
    """`- id: ping` with a `bin:` and nothing else is the drop-in that tried to
    change the binary. It gets the message and nothing more: normalize_recipe's
    stand-in `default` preset must not land as a runnable bare `ping`."""
    cat = _catalog(tmp_path, {"a.yaml": """
        recipes:
          - id: ping
            bin: /opt/ping
        """}, base=PING)

    assert _one(cat.errors, "ping: bin ignored")
    assert [p["id"] for p in _tool(cat, "ping")["presets"]] == ["quick"]
    assert _tool(cat, "ping")["bin"] == "true"


def test_an_empty_presets_list_is_not_a_recipe_either(tmp_path: Path):
    """The nudge in the `bin ignored` message gets an operator to write
    `presets:` and leave it empty; that is still nothing to add."""
    for tail in ("presets: []", "presets:"):
        cat = _catalog(tmp_path, {"a.yaml": f"""
            recipes:
              - id: ping
                bin: /opt/ping
                {tail}
            """}, base=PING)
        assert [p["id"] for p in _tool(cat, "ping")["presets"]] == ["quick"], tail
        assert cat.dropin_variant_count("a.yaml") == 0, tail



CHAIN_TWICE = """
    recipes: []
    chains:
      - id: reach
        steps: [ping/quick]
      - id: reach
        name: the second one
        steps: [ping/quick]
    """

BASE_WITH_CHAIN_TWICE = """
    recipes:
      - id: ping
        bin: true
        presets:
          - id: quick
            flags: "-c 1"
    chains:
      - id: reach
        steps: [ping/quick]
      - id: reach
        name: the second one
        steps: [ping/quick]
    """

CHAIN_ONCE = """
    recipes: []
    chains:
      - id: reach
        steps: [ping/quick]
    """


def test_a_chain_id_repeated_in_one_file_is_one_chain_and_a_message(tmp_path: Path):
    from fieldlog.recipes import find_chain

    cat = _catalog(tmp_path, {"a.yaml": CHAIN_TWICE}, base=PING)
    assert [c["id"] for c in cat.chains] == ["reach"]
    assert find_chain(cat, "reach")["name"] == "the second one"
    assert _one(cat.errors, "chain reach defined twice") == (
        "recipes.d/a.yaml: chain reach defined twice · last one kept"
    )
    assert [m for m in cat.overrides if "chain" in m] == []


def test_the_base_file_cannot_hold_two_chains_with_one_id(tmp_path: Path):
    from fieldlog.recipes import find_chain

    cat = _catalog(tmp_path, {}, base=BASE_WITH_CHAIN_TWICE)
    assert [c["id"] for c in cat.chains] == ["reach"]
    assert find_chain(cat, "reach")["name"] == "the second one"
    assert _one(cat.errors, "chain reach defined twice")


def test_across_files_a_chain_repeat_is_an_override(tmp_path: Path):
    later = CHAIN_ONCE.replace("- id: reach\n", "- id: reach\n        name: later\n")
    cat = _catalog(tmp_path, {"a.yaml": CHAIN_ONCE, "b.yaml": later}, base=PING)
    assert [c["id"] for c in cat.chains] == ["reach"]
    assert cat.chains[0]["name"] == "later"
    assert "chain reach overridden by recipes.d/b.yaml" in cat.overrides
    assert [m for m in cat.errors if "chain" in m] == []
