"""The args-band parsers in app.py: pure functions, no Textual harness needed.

`tokenize` shell-splits an argument string while keeping quoted substrings whole
(quotes included), and `arg_groups` pairs each flag with the values that follow
it — what the TUI shows in the ARGS band before a run.
"""

from __future__ import annotations

import pytest

from fieldlog.app import arg_groups, tokenize


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
