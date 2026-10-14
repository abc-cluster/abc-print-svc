"""The title page's identity, validated before a build starts.

The pipeline reads these from a `thesis.yml` beside the chapters and refuses an
unknown key. That refusal arrives thirty seconds into a render, inside a job log,
which is the wrong place for "you misspelled supervisor". The same rules are
applied here so a caller gets a 400 naming the problem at submission.

Why refusing matters more than it looks: every one of these keys has a fallback
inside the typst template. A key that is dropped — because it was misspelled, or
because it is not one the template reads — does not produce an error or a blank.
It produces the TEMPLATE'S value, which is somebody else's name on a title page
that otherwise looks entirely correct.
"""
from __future__ import annotations

# Closed set, matching ALLOWED in the pipeline's thesis-su-pdf.sh. Kept in the same
# order so the two lists can be read side by side.
KEYS = (
    "author", "degree", "department", "faculty", "supervisor", "award-date",
    "declaration-date", "papers-published", "papers-unpublished", "copyright-year",
)

MAX_LEN = 300
FILENAME = "thesis.yml"


class IdentityError(ValueError):
    pass


def parse(text: str) -> dict:
    """Validate a thesis.yml body and return its keys. Raises IdentityError."""
    import yaml

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise IdentityError(f"{FILENAME} is not valid YAML: {exc}")
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise IdentityError(f"{FILENAME} must be a YAML mapping")

    unknown = sorted(k for k in data if k not in KEYS)
    if unknown:
        raise IdentityError(
            f"{FILENAME} has unknown key(s) {unknown}; the template has a fallback "
            f"for every field, so an unrecognised key would silently leave the "
            f"template's own value on the title page. Allowed: {list(KEYS)}")

    out: dict = {}
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, (list, dict, bool)):
            raise IdentityError(f"{FILENAME}: {k!r} must be a single value, not {type(v).__name__}")
        s = str(v).strip()
        if not s:
            continue
        if "\n" in s or "\r" in s:
            raise IdentityError(f"{FILENAME}: {k!r} spans several lines")
        if len(s) > MAX_LEN:
            raise IdentityError(f"{FILENAME}: {k!r} is {len(s)} characters, limit is {MAX_LEN}")
        # The author reaches a typst STRING literal (set document(author: …) takes a
        # string, not content), and a template has no way to escape one. A quote or a
        # backslash there is a syntax error in generated typst, several steps away
        # from the person who typed it.
        bad = [c for c in ('"', "\\") if c in s]
        if bad:
            raise IdentityError(
                f"{FILENAME}: {k!r} contains {bad[0]!r}, which cannot be escaped in the "
                f"generated typst")
        out[k] = s
    return out


def read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return parse(fh.read())
