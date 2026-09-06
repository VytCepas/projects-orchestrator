"""Scheme and host allowlist for descriptor- and operator-declared URLs.

A child repo's ``.agents/config.yaml`` is untrusted input — every descriptor
docstring in this codebase says so — and ``ci.status_url`` was read from it and
passed straight into :func:`urllib.request.urlopen`, which honours ``file://``,
``ftp://`` and friends. Measured before this guard existed: a descriptor
declaring ``status_url: file:///etc/passwd`` returned ``pass``, having actually
read the file. A read-only fleet probe was a local-file-read primitive.

ONE definition, adopted by all three call sites. ``adapters/cloud.py`` already
carried its own copy of the scheme check; #181 is open precisely because the
atomic-write pattern was duplicated four times and the copies then diverged, so
a second spelling of a *security* predicate is not a trade worth repeating.

WHAT IS REFUSED, AND WHAT DELIBERATELY IS NOT
---------------------------------------------

- **Scheme**: ``http`` and ``https`` only. Everything else is refused.
- **Link-local literals** (``169.254.0.0/16``, ``fe80::/10``, and — via the
  stdlib's own handling, not ours — the IPv4-mapped spelling) are refused:
  ``169.254.169.254`` is the AWS/GCP instance-metadata endpoint, and this fleet
  runs on machines that may hold cloud credentials (ADR-012).
- **Loopback and RFC1918 private ranges are ALLOWED**, deliberately. A
  self-hosted Jenkins or Buildkite reachable only on a private LAN is exactly
  the case this adapter exists for (project-init #828). Refusing those would
  delete the feature in order to close a hole the operator opened on purpose.
- **A hostname that RESOLVES to a link-local address is NOT refused.** Resolving
  it here would put a DNS round-trip inside a pure predicate, and the verdict
  would still be racy against the ``urlopen`` that follows — the name can
  resolve differently the second time. The limit is stated rather than papered
  over: this guard stops the scheme class and the literal metadata address, not
  DNS-based SSRF. Tightening it needs a resolve-then-connect-to-the-same-address
  transport, which is a different change from this one.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

#: The only schemes a fleet probe may fetch. ``file``/``ftp``/``data`` are the
#: ones that make an arbitrary URL dangerous rather than merely useless.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _literal_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse ``host`` as an IP literal; ``None`` when it is a name (pure).

    A name is allowed by the caller, per the module docstring.

    This carried an IPv4-mapped unwrap (``::ffff:169.254.169.254`` →
    ``169.254.169.254``) on the assumption that
    :attr:`IPv6Address.is_link_local` tests only ``fe80::/10``. **That was
    wrong** — the stdlib already reports the mapped address's verdict, so the
    unwrap was dead code and a mutation test proved it: deleting it changed no
    test outcome. Removed rather than kept as reassurance.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def is_probe_safe(url: str) -> bool:
    """Whether ``url`` may be fetched by a fleet probe (pure; never raises).

    Args:
        url: A descriptor-declared or operator-supplied URL. Arbitrary text —
            it reaches here straight from a child repo's ``config.yaml`` or the
            command line, so every failure mode must be a ``False``, not a
            raise.

    Returns:
        ``True`` only for an http/https URL whose host is either a name or a
        non-link-local IP literal. A malformed URL, an unparseable host, a
        missing host, and any other scheme are all ``False``.
    """
    try:
        parts = urlsplit(url)
        scheme = parts.scheme.lower()
        host = parts.hostname
        # `.port` is accessed for its SIDE EFFECT of validating: urlsplit and
        # `.hostname` both tolerate `http://host:notaport/`, and only `.port`
        # raises on it. Without this line the predicate called that URL safe —
        # caught by test_malformed_or_hostless_urls_are_refused, not by review.
        _ = parts.port
    except ValueError:
        # Malformed authority — an unbracketed IPv6 literal, a port that is not
        # a number or is out of range. Refuse rather than guess at the intent.
        return False
    if scheme not in _ALLOWED_SCHEMES:
        return False
    if not host:
        return False
    address = _literal_host(host)
    if address is None:
        return True
    return not address.is_link_local
