"""`[!] runnable` hides what is not installed, and nothing else.

Reported as: "I assumed typing `openssl 14` would hit on openssl variant 'fail
if cert expires within 14 days'". It matched, and was then dropped: the variant
uses `$HOST` and the target was an address. The tree still listed `openssl … 6v`,
since a tool row only asks whether its binary is installed, so the filter denied
a variant the tree was showing. Pinned, Recent and the Chains section dropped
scope-blocked rows the same way, and with no target set the Chains section
showed only the chain that needs none.

A recipe or chain waiting on a scope value stays listed, dimmed, and its run
button says what it wants. Only a missing binary hides a row — the rule
`fieldlog list -r` always applied.
"""

from __future__ import annotations

from pathlib import Path

from fieldlog.app import FieldlogApp
from fieldlog.recipes import search
from fieldlog.state import TargetSession

ABSENT = "fieldlog-no-such-binary"

TOOLS = [
    {
        "id": "alpha", "bin": "true", "name": "Alpha",
        "presets": [
            {"id": "plain", "name": "plain", "flags": ""},
            {"id": "named", "name": "wants a dns name", "flags": "$HOST"},
        ],
    },
    {
        "id": "absent", "bin": ABSENT, "name": "Absent",
        "presets": [{"id": "gone", "name": "never installed", "flags": ""}],
    },
]

CHAINS = [
    {"id": "wants-host", "name": "waits on the scope",
     "steps": [{"recipe": "alpha/named", "continue": False}]},
    {"id": "wants-absent", "name": "waits on an install",
     "steps": [{"recipe": "absent/gone", "continue": False}]},
    # check_chain answers with the first blocked step, the dns name here; the
    # install step 2 waits on is what has to hide it.
    {"id": "host-then-absent", "name": "both",
     "steps": [{"recipe": "alpha/named", "continue": False}, {"recipe": "absent/gone", "continue": False}]},
]


def _app(workspace: Path) -> FieldlogApp:
    session = TargetSession(target="10.0.0.1", interface="eth0")    # an address: no dns name
    session.workspace_dir = workspace
    app = FieldlogApp(session)
    app.catalog.tools = TOOLS
    app.catalog.chains = CHAINS
    app._recipes = TOOLS
    return app


def _between(app: FieldlogApp, first: str, last: str) -> list:
    """`(name, blocked)` for the rows under heading `first`, up to heading `last`."""
    heads = {r.label: i for i, r in enumerate(app._rows) if r.kind == "header"}
    rows = app._rows[heads[first] + 1:heads[last]]
    return [(r.id if r.kind == "chain" else f"{r.tool_id}/{r.preset_id}", r.blocked) for r in rows]


def test_search_keeps_what_waits_on_the_scope_and_drops_what_is_not_installed():
    def blocked(t: dict, p: dict) -> bool:
        return "$HOST" in p["flags"] or t["bin"] == ABSENT

    hits = [(f"{t['id']}/{p['id']}", b) for t, p, b in search(TOOLS, "", blocked, hide_missing=True)]
    assert hits == [("alpha/plain", False), ("alpha/named", True)]
    everything = [f"{t['id']}/{p['id']}" for t, p, _ in search(TOOLS, "", blocked, hide_missing=False)]
    assert "absent/gone" in everything


async def test_the_filter_finds_a_variant_that_waits_on_a_dns_name(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        assert app.hide_missing, "runnable-only is the default"
        app.filter_text = "named"
        app._rebuild_tree()
        await pilot.pause()
        hits = [(r.tool_id, r.preset_id, r.blocked) for r in app._rows if r.kind == "entry"]
        assert hits == [("alpha", "named", True)], "listed, and dimmed as blocked"


async def test_chains_hide_on_a_step_not_installed_and_not_on_the_scope(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        chains = [(r.id, r.blocked) for r in app._rows if r.kind == "chain"]
        assert chains == [("wants-host", True)]

        app.action_toggle_hide_missing()            # [!] all
        await pilot.pause()
        assert [r.id for r in app._rows if r.kind == "chain"] == ["wants-host", "wants-absent", "host-then-absent"]


async def test_a_pin_waiting_on_the_scope_stays_pinned(tmp_workspace: Path):
    app = _app(tmp_workspace)
    async with app.run_test(size=(140, 45)) as pilot:
        app.pinned = ["alpha/named", "absent/gone", "chain/wants-host", "chain/wants-absent"]
        app._rebuild_tree()
        await pilot.pause()
        assert _between(app, "Pinned", "Recent") == [("alpha/named", True), ("wants-host", True)]
