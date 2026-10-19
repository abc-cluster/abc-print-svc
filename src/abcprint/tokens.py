"""Mint a token for the print service.

    python -m abcprint.tokens new jorge-logseq --scopes compile,read
    python -m abcprint.tokens check            # what the running config accepts

The plaintext is printed once and never stored: the service keeps only the hash,
so nothing can recover it afterwards. That is deliberate, and it is why this tool
prints the config entry alongside it rather than making you derive one.
"""
from __future__ import annotations

import argparse
import sys

from . import auth


def _new(args) -> int:
    scopes = tuple(s.strip() for s in args.scopes.split(",") if s.strip())
    try:
        token, entry = auth.new_token(args.label, scopes)
    except auth.AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("Token (shown once — the service stores only its hash):\n")
    print(f"  {token}\n")
    print(f"Add this entry to {auth.ENV_VAR}:\n")
    print(f"  {entry}\n")
    print("Several entries are separated by newlines or '|'. On the cluster:\n")
    print(f"  abc app config abc-print-svc set {auth.ENV_VAR}='{entry}'\n")
    print("Give the holder the token, not the entry. If it is lost, mint a new one")
    print("and remove the old entry — it cannot be recovered.")
    return 0


def _check(args) -> int:
    try:
        table = auth.load()
    except auth.AuthError as exc:
        print(f"{auth.ENV_VAR} is malformed: {exc}", file=sys.stderr)
        return 1
    if auth.disabled():
        print(f"{auth.DISABLE_VAR}=disabled — AUTHENTICATION IS OFF; every caller is "
              f"'anonymous' and shares one job view.")
        return 0
    if not table:
        print(f"{auth.ENV_VAR} is empty — the service will refuse every request "
              f"except /health and the docs.")
        return 0
    print(f"{len(table)} token(s) configured:")
    for digest, principal in table.items():
        print(f"  {principal.label:<24} {','.join(principal.scopes):<22} {digest[:12]}…")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m abcprint.tokens", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="mint a token and print its config entry")
    n.add_argument("label", help="who it is for, e.g. jorge-logseq (no ':' or spaces)")
    n.add_argument("--scopes", default=",".join(auth.SCOPES),
                   help=f"comma-separated; any of {','.join(auth.SCOPES)} "
                        f"(default: all)")
    n.set_defaults(fn=_new)

    c = sub.add_parser("check", help="report what the current environment accepts")
    c.set_defaults(fn=_check)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
