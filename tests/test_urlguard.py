"""The URL allowlist: which probe targets are refused, and which are deliberately not."""

from __future__ import annotations

import pytest

from projects_orchestrator.urlguard import is_probe_safe

# --- Allowed: the shapes a real self-hosted CI endpoint actually takes ---


@pytest.mark.parametrize(
    "url",
    [
        "http://ci.example/api",
        "https://ci.example/api",
        "HTTPS://ci.example/api",
        "https://ci.example:8443/api?job=main",
    ],
)
def test_http_family_urls_are_probe_safe(url: str) -> None:
    assert is_probe_safe(url) is True


def test_loopback_is_allowed_because_self_hosted_ci_lives_there() -> None:
    assert is_probe_safe("http://127.0.0.1:8080/api") is True


def test_private_range_is_allowed_because_a_lan_runner_lives_there() -> None:
    assert is_probe_safe("http://10.0.0.5/api") is True


# --- Refused: the scheme class that made an arbitrary URL dangerous ---


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://host/path",
        "data:application/json,{}",
        "gopher://host/1",
        "jar:file:///x!/y",
    ],
)
def test_non_http_schemes_are_refused(url: str) -> None:
    assert is_probe_safe(url) is False


# --- Refused: the instance-metadata endpoint, in each of its spellings ---


def test_ipv4_metadata_address_is_refused() -> None:
    assert is_probe_safe("http://169.254.169.254/latest/meta-data/") is False


def test_ipv6_link_local_address_is_refused() -> None:
    assert is_probe_safe("http://[fe80::1]/api") is False


def test_ipv4_mapped_metadata_address_is_refused() -> None:
    """``::ffff:169.254.169.254`` reaches the same endpoint, and is refused.

    This pins STDLIB behaviour, not ours: :attr:`IPv6Address.is_link_local`
    already reports the mapped address's verdict. Kept because the guarantee is
    load-bearing and the mechanism is not obvious — if this predicate is ever
    rewritten to check ranges by hand, this is the case that will be missed.
    """
    assert is_probe_safe("http://[::ffff:169.254.169.254]/latest/meta-data/") is False


# --- Refused: malformed input, because this predicate must never raise ---


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not a url",
        "http:///no-host",
        "http://[::1",
        "http://host:notaport/",
        "//ci.example/api",
    ],
)
def test_malformed_or_hostless_urls_are_refused(url: str) -> None:
    assert is_probe_safe(url) is False


# --- The documented limit, pinned so nobody mistakes it for more ---


def test_a_hostname_is_allowed_even_though_it_could_resolve_to_link_local() -> None:
    """The stated limit of this guard, asserted rather than left to prose.

    Refusing this would need a resolve-then-connect-to-that-address transport;
    a DNS lookup inside a pure predicate would be racy against the ``urlopen``
    that follows. If this test ever flips, the module docstring is stale.
    """
    assert is_probe_safe("http://metadata.google.internal/computeMetadata/v1/") is True
