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

TWO WAYS PAST THE FIRST VERSION OF THIS GUARD, both found in review of PR #225
and both reproduced before being fixed. They are why the host test is not just
"does it parse as a link-local literal":

1. **The resolver accepts numeric forms the strict parser rejects.**
   ``ipaddress.ip_address("2852039166")`` raises, so the host was classified as
   a NAME and allowed — while ``getaddrinfo("2852039166")`` returns
   ``169.254.169.254``. Measured. The same held for ``0xa9fea9fe``,
   ``0251.0376.0251.0376`` and the partial-dotted ``169.254.43518``. So a host
   that LOOKS numeric must parse as a strict literal or be refused: a real
   hostname always carries a character that is neither a digit nor a dot.
2. **Percent-escapes survive parsing but not connecting.**
   ``urlsplit("http://169%2e254%2e169%2e254/").hostname`` keeps the escapes, so
   again "a name" — while ``Request`` decodes the authority and connects to the
   metadata address. Measured. An escape in the authority has no legitimate use
   here (IDN travels as punycode), so any ``%`` in the host is refused.

WHAT IS REFUSED, AND WHAT DELIBERATELY IS NOT
---------------------------------------------

- **Scheme**: ``http`` and ``https`` only. Everything else is refused.
- **Link-local literals** (``169.254.0.0/16``, ``fe80::/10``, and — via the
  stdlib's own handling, not ours — the IPv4-mapped spelling) are refused:
  ``169.254.169.254`` is the AWS/GCP instance-metadata endpoint, and this fleet
  runs on machines that may hold cloud credentials (ADR-012).
- **Numeric-looking hosts that are not strict literals**, and **any host
  containing a percent-escape** — the two bypasses above.
- **Loopback and RFC1918 private ranges are ALLOWED**, deliberately. A
  self-hosted Jenkins or Buildkite reachable only on a private LAN is exactly
  the case this adapter exists for (project-init #828). Refusing those would
  delete the feature in order to close a hole the operator opened on purpose.
- **A hostname that RESOLVES to a link-local address is NOT refused.** Resolving
  it here would put a DNS round-trip inside a pure predicate, and the verdict
  would still be racy against the ``urlopen`` that follows — the name can
  resolve differently the second time. The limit is stated rather than papered
  over: this guard stops the scheme class, the literal metadata address and its
  obfuscations, not DNS-based SSRF. Closing that needs a
  resolve-then-connect-to-the-same-address transport, which is a different
  change from this one.

REDIRECTS ARE A SECOND ENTRY POINT, so the guard is not only a predicate.
Validating the declared URL protects nothing on its own: the default opener
follows a 30x automatically, and a child-controlled endpoint can answer
``Location: http://169.254.169.254/…``. Measured — the hop was followed and the
predicate was never consulted. :func:`guarded_opener` re-runs the guard on every
redirect target and refuses the hop otherwise.
"""

from __future__ import annotations

import functools
import ipaddress
import re
import urllib.request
from urllib.parse import urlsplit

#: The only schemes a fleet probe may fetch. ``file``/``ftp``/``data`` are the
#: ones that make an arbitrary URL dangerous rather than merely useless.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: A host made only of digits and dots. Such a host is an IPv4 address in *some*
#: notation the resolver understands, so it must parse strictly or be refused —
#: see bypass 1 in the module docstring. A real hostname always has a character
#: outside this set.
_NUMERIC_HOST = re.compile(r"[0-9.]+")

#: The hex spelling (``0xa9fea9fe``). Same family as above; called out
#: separately because it carries letters and so escapes ``_NUMERIC_HOST``.
_HEX_HOST = re.compile(r"0[xX][0-9a-fA-F]+")


def _literal_host(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse ``host`` as an IP literal; ``None`` when it is not one (pure).

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
        ``True`` only for an http/https URL whose host is either a real name or
        a non-link-local IP literal. A malformed URL, an unparseable host, a
        missing host, a percent-escaped authority, a numeric host in a
        non-canonical notation, and any other scheme are all ``False``.
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
    if "%" in host:
        return False
    if _HEX_HOST.fullmatch(host):
        return False
    address = _literal_host(host)
    if address is None:
        # Not a strict literal. Genuine names are fine; a digits-and-dots host
        # that the strict parser refused is a notation the RESOLVER still
        # accepts, so it does not get to pass as a name.
        return not _NUMERIC_HOST.fullmatch(host)
    return not address.is_link_local


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-run :func:`is_probe_safe` on each redirect target before following it."""

    def redirect_request(  # noqa: PLR0913 — signature fixed by the stdlib base class
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        """Return ``None`` to refuse the hop, which surfaces as the original 30x.

        Callers already map an :class:`~urllib.error.HTTPError` to their
        "could not tell" state, so a refused redirect degrades exactly like an
        unreachable endpoint rather than raising into the fleet render.
        """
        if not is_probe_safe(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


@functools.cache
def guarded_opener() -> urllib.request.OpenerDirector:
    """An opener that validates every redirect hop, not just the first URL.

    Cached: an opener is reusable and stateless here, and building one per probe
    would allocate a handler chain on every fleet render.
    """
    return urllib.request.build_opener(_GuardedRedirectHandler)
