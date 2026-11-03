"""Parse an authenticated principal from saved Muse Next Flight HTML.

This module performs no network/Store operations and has no printing CLI.
The caller must verify the HTTPS capture's provenance and retain returned raw
principals privately; publish only a round-scoped keyed fingerprint.
"""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re


class IdentityUnverified(ValueError):
    """The captured page does not unambiguously identify a signed-in user."""

    def __init__(self, message, *, reason="missing"):
        super().__init__(message)
        self.reason = reason


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = []
            self.scripts.append(self.current)

    def handle_endtag(self, tag):
        if tag == "script":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)


def authenticated_viewer_id(html: str) -> str:
    """Return the private viewer ID only when authenticated server fields agree.

    Muse currently serializes viewerId/isAuthenticated in AuthContextProvider
    props and repeats that principal in prelude.user.identifier (kind=user).
    Flight record labels and script positions are deliberately not hard-coded.
    Missing authentication, ambiguous identities, and anonymous/zero IDs fail
    closed. No other props, including tokens or session bindings, are returned.
    """
    if not isinstance(html, str):
        raise IdentityUnverified("Expected a captured HTML string")
    parser = _Scripts()
    parser.feed(html)
    decoder = json.JSONDecoder()
    chunks = []
    for parts in parser.scripts:
        source = "".join(parts)
        for match in re.finditer(r"__next_f\.push\s*\(", source):
            try:
                row, _ = decoder.raw_decode(source[match.end():].lstrip())
            except json.JSONDecodeError:
                continue
            if (isinstance(row, list) and len(row) > 1 and row[0] == 1
                    and isinstance(row[1], str)):
                chunks.append(row[1])

    viewers, prelude_users = set(), set()
    invalid_authenticated_id = False

    def visit(value):
        nonlocal invalid_authenticated_id
        if isinstance(value, dict):
            if "viewerId" in value and value.get("isAuthenticated") is True:
                principal = value["viewerId"]
                if (isinstance(principal, str) and principal.isascii()
                        and principal.isdecimal() and any(c != "0" for c in principal)):
                    viewers.add(principal)
                else:
                    invalid_authenticated_id = True
            prelude = value.get("prelude")
            user = prelude.get("user") if isinstance(prelude, dict) else None
            if (isinstance(user, dict) and user.get("kind") == "user"
                    and isinstance(user.get("identifier"), str)):
                prelude_users.add(user["identifier"])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for record in "".join(chunks).splitlines():
        label, separator, payload = record.partition(":")
        if separator and re.fullmatch(r"[0-9a-fA-F]+", label):
            try:
                visit(json.loads(payload))
            except json.JSONDecodeError:
                continue
    if invalid_authenticated_id:
        raise IdentityUnverified("Authenticated principal invalid", reason="invalid")
    if len(viewers) > 1 or len(prelude_users) > 1 or (viewers and prelude_users and prelude_users != viewers):
        raise IdentityUnverified("Authenticated principals conflict", reason="conflict")
    if len(viewers) != 1 or prelude_users != viewers:
        raise IdentityUnverified("Authenticated principal missing")
    return next(iter(viewers))
