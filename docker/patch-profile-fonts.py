#!/usr/bin/env python3
"""Append libre fallbacks to the profile's typst font stacks.

The template declares fonts as typst fallback LISTS, e.g.

    #set text(font: ("Calibri", "Arial", "Helvetica Neue", ...))

Typst walks that list and uses the first family it can see. In an image that
ships no proprietary fonts it sees NONE of them, falls off the end of the list,
and silently renders in its own default (Libertinus) — which is not a
substitution at all, and is how a build can come out five pages shorter with the
service still reporting healthy.

Installing Carlito is not enough on its own: typst matches on family NAME and
does not read fontconfig, so a request for "Calibri" never reaches "Carlito".
The name has to appear in the stack. This inserts each libre equivalent directly
after the family it stands in for, which keeps the licensed font winning when it
is mounted and makes the substitute the next choice rather than the default.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# requested -> metric-compatible libre equivalent
EQUIV = {
    "Calibri": "Carlito",
    "Cambria": "Caladea",
    "Georgia": "Gelasio",
    "Times New Roman": "Tinos",
    "Arial": "Arimo",
    "Helvetica Neue": "Arimo",
}

FONT_LIST = re.compile(r'font:\s*\(([^)]*)\)')


def patch_list(inner: str) -> str:
    names = [n.strip() for n in inner.split(",")]
    out: list[str] = []
    for raw in names:
        if not raw:
            continue
        out.append(raw)
        fam = raw.strip().strip('"')
        sub = EQUIV.get(fam)
        if sub:
            quoted = f'"{sub}"'
            if quoted not in names and quoted not in out:
                out.append(quoted)
    return ", ".join(out)


def main(root: str) -> int:
    changed = 0
    for path in Path(root).rglob("*.typ"):
        text = path.read_text(encoding="utf-8", errors="replace")
        new = FONT_LIST.sub(lambda m: f"font: ({patch_list(m.group(1))})", text)
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed += 1
            print(f"  patched font stacks in {path.relative_to(root)}")
    if not changed:
        # Fail loudly: a profile whose stacks were not patched renders in the
        # wrong font while everything else reports fine.
        print("FATAL: no typst font stacks were patched; the profile layout changed", file=sys.stderr)
        return 1
    print(f"patched {changed} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "profiles/su-fmhs"))
