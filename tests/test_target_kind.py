"""What counts as an address, and what is a hostname that merely looks like one.

`dc1`, `cafe` and `db2` are spelled entirely in hex digits. Read as addresses,
they lose their `$HOST` binding and every dns recipe refuses to run against them.
"""

from __future__ import annotations

import pytest

from fieldlog.state import TargetSession


@pytest.mark.parametrize("target", ["dc1", "cafe", "db2", "ad", "fe1", "beef.local", "router1"])
def test_a_hostname_spelled_in_hex_is_still_a_hostname(target: str):
    session = TargetSession(target=target)
    assert session.target_kind == "hostname"
    assert session.dns_name == target


@pytest.mark.parametrize("target", ["10.0.0.1", "fe80::1", "::1", "2001:db8::1", "192.168.1.20"])
def test_a_literal_address_stands_in_for_no_dns_name(target: str):
    session = TargetSession(target=target)
    assert session.target_kind == "address"
    assert session.dns_name == ""


def test_a_cidr_is_a_subnet():
    assert TargetSession(target="10.0.0.0/24").target_kind == "subnet"


def test_an_ssh_target_is_its_own_kind():
    assert TargetSession(target="operator@jump1").target_kind == "user@host"


@pytest.mark.parametrize("target", ["1.2.3", "10.0.0.256", "192.168.001.020"])
def test_a_mistyped_address_is_not_read_as_a_hostname(target: str):
    """Digits and dots is someone typing an address. Read as a hostname it
    would start binding `$HOST` and dns recipes would run against a typo."""
    session = TargetSession(target=target)
    assert session.target_kind == "address"
    assert session.dns_name == ""
