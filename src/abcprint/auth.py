"""Bearer-token authentication, and the ownership that makes it mean something.

A lock on the front door was not the whole problem. Before this existed the
service was reachable unauthenticated on the public internet, and `GET /jobs`
enumerated every caller's jobs while every artifact was fetchable by id — so one
caller could download another's compiled document. A shared secret would have
closed the door and left that second hole wide open. Hence: every job records the
token that created it, and a token sees only its own.

The posture that caused the original bug is worth naming, because it is easy to
repeat. `abc-app.yaml` claimed CORS could be permissive "because the platform
auth-gates the app (access: team)". That was never tested, and it was false. CORS
constrains browsers, not curl, so it was never the control either. The control is
this module.

Tokens are stored HASHED. The plaintext exists only in whoever holds it, and
`new_token()` is the one place it is ever printed.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets

ENV_VAR = "ABC_PRINT_SVC_TOKEN"
DISABLE_VAR = "ABC_PRINT_SVC_AUTH"

# A token carries what it may do, so a plugin that only needs to compile and poll
# is not also able to write to the holder's object store.
SCOPES = ("compile", "read", "deliver")
PREFIX = "abcp_"           # so a leaked string is recognisable in a log or a repo
_HASH = "sha256"


class AuthError(ValueError):
    pass


class Principal:
    """Who is calling, and what they may do."""

    __slots__ = ("label", "scopes")

    def __init__(self, label: str, scopes: tuple[str, ...]):
        self.label = label
        self.scopes = scopes

    def may(self, scope: str) -> bool:
        return scope in self.scopes

    def __repr__(self) -> str:                      # never includes a token
        return f"Principal(label={self.label!r}, scopes={self.scopes!r})"


# The principal a request gets when authentication is deliberately switched off.
# It owns its jobs like any other, so isolation still behaves consistently and a
# development run is not a different code path.
ANONYMOUS = Principal("anonymous", SCOPES)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(label: str, scopes: tuple[str, ...] = SCOPES) -> tuple[str, str]:
    """Mint a token. Returns (plaintext, config entry). The plaintext is not stored.

    Printed once, by whoever runs this, and never recoverable afterwards — which is
    the point of keeping only the hash.
    """
    bad = [s for s in scopes if s not in SCOPES]
    if bad:
        raise AuthError(f"unknown scope(s) {bad}; allowed: {list(SCOPES)}")
    if not label or any(c in label for c in ":;|= \t\n"):
        raise AuthError(
            "a label must be non-empty and free of ':', ';', '|', '=' and whitespace, "
            "because it is a field in the config entry")
    token = PREFIX + secrets.token_urlsafe(32)
    entry = f"{label}:{','.join(scopes)}:{_HASH}:{_digest(token)}"
    return token, entry


def _parse_entry(raw: str) -> tuple[str, Principal]:
    """One `label:scopes:sha256:hex` entry. Returns (hex digest, principal)."""
    parts = raw.split(":")
    if len(parts) == 2 and parts[0] == _HASH:
        # Bare `sha256:<hex>` — one token, every scope. The env var is named in the
        # singular and one token is the common case, so this shape is accepted.
        label, scopes, algo, digest = "default", SCOPES, parts[0], parts[1]
    elif len(parts) == 4:
        label, scope_s, algo, digest = parts
        scopes = tuple(s for s in scope_s.split(",") if s)
    else:
        raise AuthError(
            f"token entry {raw[:24]!r}… is not `label:scopes:sha256:hex` "
            f"(or bare `sha256:hex`)")
    if algo != _HASH:
        raise AuthError(f"token entry for {label!r} uses {algo!r}; only {_HASH} is supported")
    digest = digest.strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise AuthError(
            f"token entry for {label!r} does not carry a {_HASH} hex digest. "
            f"The env var holds HASHES, not tokens — mint one with "
            f"`python -m abcprint.tokens new <label>`")
    unknown = [s for s in scopes if s not in SCOPES]
    if unknown:
        raise AuthError(f"token entry for {label!r} has unknown scope(s) {unknown}; "
                        f"allowed: {list(SCOPES)}")
    if not scopes:
        raise AuthError(f"token entry for {label!r} grants no scopes")
    return digest, Principal(label, scopes)


def load(env: dict | None = None) -> dict[str, Principal]:
    """Read the configured tokens. Raises AuthError on anything malformed.

    Malformed configuration is fatal rather than skipped: an entry quietly dropped
    for a typo is a token that stops working with no explanation, or — worse, if it
    were the only one — a service that comes up accepting nobody and looks healthy.
    """
    src = os.environ if env is None else env
    raw = (src.get(ENV_VAR) or "").strip()
    if not raw:
        return {}
    out: dict[str, Principal] = {}
    for chunk in raw.replace("|", "\n").splitlines():
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        digest, principal = _parse_entry(chunk)
        if digest in out:
            raise AuthError(
                f"two token entries share one hash ({out[digest].label!r} and "
                f"{principal.label!r}); one of them would never match")
        out[digest] = principal
    return out


def disabled(env: dict | None = None) -> bool:
    src = os.environ if env is None else env
    return (src.get(DISABLE_VAR) or "").strip().lower() == "disabled"


def verify(token: str | None, table: dict[str, Principal]) -> Principal | None:
    """Match a presented token against the table in constant time.

    Compared with hmac.compare_digest against every entry rather than by dict
    lookup: a dict lookup on the digest is already constant-time-ish, but the
    comparison is written explicitly so it cannot regress into `==` later.
    """
    if not token:
        return None
    presented = _digest(token)
    found = None
    for digest, principal in table.items():
        if hmac.compare_digest(presented, digest):
            found = principal
    return found


def bearer(header: str | None) -> str | None:
    """Pull the token out of an Authorization header."""
    if not header:
        return None
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None
