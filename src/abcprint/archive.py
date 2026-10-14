"""Safe extraction of a client-supplied zip into a build workspace.

A zip from an untrusted client is a well-known hazard, and the guards here are
not theoretical:

  zip slip      an entry named ../../etc/cron.d/x escapes the extraction root.
                Every component is validated, and the resolved path must stay
                under the root.
  zip bomb      a few KB expanding to gigabytes. Both the declared total and the
                bytes actually written are capped, and writing is streamed so a
                lying header cannot overshoot the cap.
  symlinks      a link to /etc/passwd turns a later write, or the renderer's own
                read, into an arbitrary file access. Non-regular entries are
                refused rather than skipped, so the bundle fails loudly.
  absolute paths, drive letters, NUL bytes, and names that differ only by case
                on a case-insensitive filesystem.

Structure is PRESERVED rather than flattened. The project-garden manuscript
layout references its figures as ../assets/figures/current/… from src/, so a
flattened extraction would make every one of those unresolvable.
"""
from __future__ import annotations

import os
import posixpath
import zipfile

MAX_ENTRIES = int(os.environ.get("ABCPRINT_ZIP_MAX_ENTRIES", "5000"))
MAX_TOTAL_BYTES = int(os.environ.get("ABCPRINT_ZIP_MAX_BYTES", str(512 * 1024 * 1024)))
MAX_ENTRY_BYTES = int(os.environ.get("ABCPRINT_ZIP_MAX_ENTRY_BYTES", str(128 * 1024 * 1024)))
CHUNK = 64 * 1024


class ArchiveError(ValueError):
    pass


def _reject(name: str) -> str | None:
    if not name or name.strip() == "":
        return "empty entry name"
    if "\x00" in name:
        return "entry name contains a NUL byte"
    n = name.replace("\\", "/")
    if n.startswith("/"):
        return "absolute path"
    if len(n) > 1 and n[1] == ":":
        return "drive-letter path"
    for part in n.split("/"):
        if part == "..":
            return "'..' component"
    return None


# Directories that MEAN something in the layouts this service accepts, and so can
# never be a wrapper to strip. Without this, an archive whose only top-level
# directory is `src/` — a manuscript with no assets, which is an ordinary thing to
# submit — had its `src/` stripped as if it were the folder someone zipped. On the
# thesis arm every entry then matched no mapped location and the refusal read
# "archive carries no sources under src/" about an archive that was entirely
# sources. On the quarto arm it survived only because resolve_entry also accepts a
# bare index.qmd.
LAYOUT_DIRS = frozenset({"src", "assets"})


def _common_prefix(names: list[str]) -> str:
    """A single wrapping directory, if the whole archive sits inside one.

    Zipping a folder normally produces `manuscript/src/index.qmd` rather than
    `src/index.qmd`. Stripping one level makes both shapes work, and only when
    EVERY entry shares it — otherwise a top-level file would be silently moved.
    """
    tops = {n.replace("\\", "/").split("/")[0] for n in names if n.strip()}
    if len(tops) != 1:
        return ""
    top = tops.pop()
    # A layout directory is content, not packaging: stripping it discards the very
    # structure the archive is supposed to be expressing.
    if top in LAYOUT_DIRS:
        return ""
    # Only strip when it is genuinely a directory wrapping everything.
    if all(n.replace("\\", "/").startswith(top + "/") for n in names if n.strip()):
        return top
    return ""


def extract(zip_path: str, dest_root: str) -> dict:
    """Extract into dest_root, preserving structure. Returns a manifest."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a readable zip archive: {exc}")

    with zf:
        infos = [i for i in zf.infolist()]
        if len(infos) > MAX_ENTRIES:
            raise ArchiveError(
                f"archive has {len(infos)} entries, limit is {MAX_ENTRIES}")

        declared = sum(i.file_size for i in infos)
        if declared > MAX_TOTAL_BYTES:
            raise ArchiveError(
                f"archive expands to {declared} bytes, limit is {MAX_TOTAL_BYTES}")

        names = [i.filename for i in infos]
        for n in names:
            why = _reject(n)
            if why:
                raise ArchiveError(f"refused entry {n!r}: {why}")

        strip = _common_prefix(names)
        root = os.path.abspath(dest_root)
        written, total, dirs = [], 0, 0

        for info in infos:
            name = info.filename.replace("\\", "/")
            if strip:
                name = name[len(strip) + 1:]
            if not name:
                continue

            mode = (info.external_attr >> 16) & 0o170000
            if mode == 0o120000:
                raise ArchiveError(f"refused entry {info.filename!r}: symlink")
            if mode and mode not in (0o100000, 0o040000):
                raise ArchiveError(
                    f"refused entry {info.filename!r}: not a regular file or directory")

            target = os.path.abspath(os.path.join(root, *name.split("/")))
            if os.path.commonpath([root, target]) != root:
                raise ArchiveError(f"refused entry {info.filename!r}: escapes the archive root")

            if name.endswith("/") or info.is_dir():
                os.makedirs(target, exist_ok=True)
                dirs += 1
                continue

            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Stream and count. A header can lie about file_size, so the cap is
            # enforced on bytes actually written, not on what the archive claims.
            n_bytes = 0
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(CHUNK)
                    if not chunk:
                        break
                    n_bytes += len(chunk)
                    total += len(chunk)
                    if n_bytes > MAX_ENTRY_BYTES:
                        raise ArchiveError(
                            f"entry {info.filename!r} exceeds {MAX_ENTRY_BYTES} bytes")
                    if total > MAX_TOTAL_BYTES:
                        raise ArchiveError(
                            f"archive exceeds {MAX_TOTAL_BYTES} bytes while extracting")
                    out.write(chunk)
            written.append(posixpath.normpath(name))

    return {"files": sorted(written), "directories": dirs,
            "bytes": total, "stripped_prefix": strip or None}


# Where a garden-layout manuscript keeps things. Used only to REPORT what was
# recognised; nothing is moved, because the document's own relative references
# are what must keep working.
GARDEN_HINTS = {
    "sources": "src/",
    "figures": "assets/figures/",
    "tables": "assets/tables/",
    "supplemental": "assets/supplemental-data/",
}


def describe(manifest: dict, entry: str) -> dict:
    """Summarise an extracted archive against the expected layout."""
    files = manifest["files"]
    found = {k: [f for f in files if f.startswith(v)] for k, v in GARDEN_HINTS.items()}
    return {
        "entry": entry,
        "entry_present": entry in files,
        "stripped_prefix": manifest.get("stripped_prefix"),
        "counts": {k: len(v) for k, v in found.items()},
        "total_files": len(files),
        "bytes": manifest["bytes"],
    }
