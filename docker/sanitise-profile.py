#!/usr/bin/env python3
"""Replace the author's own thesis material in a vendored profile with fixtures.

The SU/FMHS profile is vendored from a private repository, and a profile carries
more than formatting rules: the dedication and acknowledgements fragments are
GENERATED FROM THE AUTHOR'S OWN WORDS, and the template's parameter defaults name
a real person, department and supervisor.

That is fine in a private image and not fine in a public one. On 2026-10-14 the
package was made public and those fragments — a dedication in verse, and
acknowledgements naming the author's parents — became downloadable by anyone.

This replaces them with the fixture front matter in examples/, regenerating the
Typst fragments with the profile's OWN converters so the format stays correct
rather than hand-faked. Formatting rules, the statutory declaration wording and
the layout machinery are untouched: those are the rulebook, which is the point of
the profile.

Idempotent, and verifies afterwards that no personal string survives.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

# Template parameter DEFAULTS that name real people and places. These are
# defaults, not machinery — a caller supplies its own — so genericising them
# removes a name without changing how anything renders.
DEFAULTS = {
    r'(author:\s*)"[^"]*"': r'\1"A. N. Author"',
    r'(department:\s*)"[^"]*"': r'\1"Department of Fixtures"',
    r'(supervisor:\s*)"[^"]*"': r'\1"Prof. A Supervisor"',
    r'(co-supervisor:\s*)"[^"]*"': r'\1""',
}

# Build-time proof documents. They embed the real front matter and are not used
# by the service, so they are dropped rather than sanitised.
DROP = ("dedication-proof.typ", "frontmatter-proof.typ", "dedication-render.typ")

# Names that appear outside the parameter defaults — in the extension's own
# metadata, in a hardcoded default inside a build script, and in a comment that
# uses a real citation as its worked example. Substituted literally, longest
# first so a full name is not half-replaced.
NAMES = [
    ("Abhinav Sharma", "A. N. Author"),
    ("Gian van der Spuy", "A Supervisor"),
    ("van der Spuy", "Supervisor"),
    ("Abhinav", "Author"),
    ("Sharma", "Author"),
]

# Anything matching these must not survive. Checked after the rewrite.
FORBIDDEN = ["Abhinav", "Sharma", "van der Spuy", "Mukesh", "Archna",
             "Manusmriti", "Vishnusmriti", "Atigurus",
             "educational endeavour", "easiest and hardest"]


def main(profile: str, fixtures: str, pipeline: str = "pipeline") -> int:
    prof, fx = pathlib.Path(profile), pathlib.Path(fixtures)
    # The PIPELINE carries names too — a comment in thesis-manuscript-su.sh
    # explains a byline rule using a real supervisor. Scrubbing only the profile
    # left it in, which an independent scan caught; the tool now covers both.
    extra = pathlib.Path(pipeline)
    if not prof.is_dir():
        print(f"sanitise: no such profile {prof}", file=sys.stderr)
        return 1

    # 1. Regenerate the front-matter fragments from fixture markdown, using the
    #    profile's own converters so the emitted Typst stays valid.
    for md, conv, out in (("Dedication.md", "md2dedication.py", "_dedication.typ"),
                          ("Acknowledgements.md", "md2acknowledgements.py", "_acknowledgements.typ")):
        src, script, dest = fx / md, prof / conv, prof / out
        if not dest.exists():
            continue
        if not (src.exists() and script.exists()):
            print(f"sanitise: cannot regenerate {out} "
                  f"(missing {src if not src.exists() else script})", file=sys.stderr)
            return 1
        subprocess.run([sys.executable, str(script), str(src), str(dest)], check=True)
        print(f"  regenerated {out} from fixture {md}")

    # 2. Drop the build-time proofs, which embed the real front matter.
    for name in DROP:
        p = prof / name
        if p.exists():
            p.unlink()
            print(f"  dropped {name} (build-time proof, embeds real front matter)")

    # 3. Genericise the template's parameter defaults.
    for typ in prof.rglob("*.typ"):
        text = original = typ.read_text(encoding="utf-8", errors="replace")
        for pat, rep in DEFAULTS.items():
            text = re.sub(pat, rep, text)
        if text != original:
            typ.write_text(text, encoding="utf-8")
            print(f"  genericised parameter defaults in {typ.relative_to(prof)}")

    # 4. Substitute remaining names in every text file. The parameter-default
    #    rules above miss the extension's own metadata, a hardcoded author in a
    #    build script, and a comment whose worked example is a real citation.
    targets = list(prof.rglob("*")) + (list(extra.rglob("*")) if extra.is_dir() else [])
    for f in targets:
        if not f.is_file() or f.suffix in (".png", ".svg", ".json"):
            continue
        try:
            text = original = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for old, new in NAMES:
            text = text.replace(old, new)
        if text != original:
            f.write_text(text, encoding="utf-8")
            print(f"  substituted names in {f}")

    # 5. Verify. A sanitiser that reports success without checking is the exact
    #    failure mode this project keeps finding, so fail loudly instead.
    leaks: list[str] = []
    for f in targets:
        if not f.is_file() or f.suffix in (".png", ".svg"):
            continue
        try:
            body = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for term in FORBIDDEN:
            if term in body:
                leaks.append(f"{f}: {term}")
    if leaks:
        print("FATAL: personal content survived sanitisation:", file=sys.stderr)
        for l in leaks:
            print(f"  {l}", file=sys.stderr)
        return 1
    print("  verified: no personal content remains in the profile")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(
        sys.argv[1] if len(sys.argv) > 1 else "profiles/su-fmhs",
        sys.argv[2] if len(sys.argv) > 2 else "examples/thesis-chapter-demo/frontmatter",
        sys.argv[3] if len(sys.argv) > 3 else "pipeline"))
