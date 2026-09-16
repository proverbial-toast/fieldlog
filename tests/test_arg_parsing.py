"""The args-band parsers in app.py: pure functions, no Textual harness needed.

`tokenize` shell-splits an argument string while keeping quoted substrings whole
(quotes included), and `arg_groups` pairs each flag with the values that follow
it — what the TUI shows in the ARGS band before a run.
"""

from __future__ import annotations

import pytest

from fieldlog.tui.helpers import arg_groups, tokenize


@pytest.mark.parametrize(
    "text, expected",
    [
        ("", []),
        ("   ", []),
        ("-c 4 -W 1 $TARGET", ["-c", "4", "-W", "1", "$TARGET"]),
        ("  spaced   out ", ["spaced", "out"]),
        ("$(id)", ["$(id)"]),
    ],
)
def test_tokenize_splits_on_unquoted_whitespace(text, expected):
    assert tokenize(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ('-w "a b" x', ["-w", '"a b"', "x"]),          # spaces inside quotes stay together
        ("-H 'Accept: */*'", ["-H", "'Accept: */*'"]),  # quote chars are preserved on the token
    ],
)
def test_tokenize_keeps_quoted_substrings_whole(text, expected):
    assert tokenize(text) == expected


def test_arg_groups_pairs_flags_with_following_values():
    result = arg_groups("-n -I eth0")
    assert result["count"] == 3
    assert result["groups"] == [
        {"flag": "-n", "value": ""},        # bare flag, next token is another flag
        {"flag": "-I", "value": "eth0"},    # flag consumes the value after it
    ]


def test_arg_groups_collects_multiple_values_for_one_flag():
    # A flag greedily takes every following token that is not itself a flag.
    result = arg_groups("-W 1 $TARGET")
    assert result["groups"] == [{"flag": "-W", "value": "1 $TARGET"}]


def test_arg_groups_treats_a_bare_word_as_a_positional():
    assert arg_groups("scan.txt")["groups"] == [{"flag": "", "value": "scan.txt"}]


def test_arg_groups_of_empty_string_is_empty():
    assert arg_groups("") == {"groups": [], "count": 0}


# ---- A fat-fingered subcommand looks like a recipe -------------------------


def test_a_near_miss_subcommand_is_named():
    """`fieldlog repot x` dispatches to `run repot` — the shorthand that makes
    `fieldlog nmap` work. Once that lookup fails, say what was probably meant."""
    from fieldlog.cli import subcommand_hint

    assert subcommand_hint("repot") == " Did you mean `fieldlog report`?"
    assert subcommand_hint("doctro") == " Did you mean `fieldlog doctor`?"
    assert subcommand_hint("histroy") == " Did you mean `fieldlog history`?"
    # A real tool name is not second-guessed.
    assert subcommand_hint("nmap") == ""
    assert subcommand_hint("traceroute") == ""
    assert subcommand_hint("") == ""


def test_the_hint_reaches_the_error(capsys):
    from fieldlog.cli import build_parser, handle_run
    from fieldlog.recipes import load_catalog

    args = build_parser().parse_args(["run", "repot", "-t", "10.0.0.1"])
    assert handle_run(args, load_catalog()) == 1
    err = capsys.readouterr().err
    assert "Unknown recipe or tool 'repot'" in err and "fieldlog report" in err
