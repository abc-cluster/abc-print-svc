"""A garden-layout zip must land in the places the thesis pipeline actually reads.

The quarto engine PRESERVES an archive's structure; the thesis pipeline cannot — it
reads fixed locations under writeup/ and a chapter references its figures as
`figures/<name>`. So this arm MAPS, and a mapping that silently puts a file somewhere
unread is the failure mode worth guarding: a per-area bibliography left beside
references.bib loses its citations with nothing but a citeproc warning, and a figure
left under assets/ renders as a missing image hundreds of pages in.
"""
import os
import zipfile

import pytest

from abcprint import workspace


def _zip(tmp_path, entries):
    z = tmp_path / "bundle.zip"
    with zipfile.ZipFile(z, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return str(z)


MINIMAL = {
    "src/01-introduction.md": "# Introduction\n",
    "src/Dedication.md": "# Dedication\n",
    "src/references.bib": "@article{a, title={A}}\n",
    "src/_ch01-refs-2026-09-23.bib": "@article{b, title={B}}\n",
    "assets/figures/current/F1.1-x.png": b"\x89PNG\r\n",
    "assets/figures/current/F6.2-src/F6.2-v4.svg": "<svg/>",
    "assets/inserted-papers/ch02-paper.pdf": b"%PDF-1.4\n",
}


@pytest.fixture
def profile(tmp_path, monkeypatch):
    """create() copies a profile and a pipeline in; stub both."""
    prof, pipe = tmp_path / "prof", tmp_path / "pipe"
    prof.mkdir(), pipe.mkdir()
    (prof / "_quarto.yml").write_text("format: {}\n")
    (pipe / "thesis-assemble.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(workspace, "PROFILE_DIR", str(prof))
    monkeypatch.setattr(workspace, "PIPELINE_DIR", str(pipe))


def test_every_part_lands_where_the_pipeline_reads_it(tmp_path, profile):
    root = str(tmp_path / "ws")
    paths, mapping, _ = workspace.create_from_archive(root, _zip(tmp_path, MINIMAL))

    assert os.path.isfile(os.path.join(paths["thesis"], "01-introduction.md"))
    assert os.path.isfile(os.path.join(paths["thesis"], "Dedication.md"))
    # references.bib beside writeup/, per-area bibs INSIDE writeup/thesis — the
    # pipeline globs writeup/thesis/_*-refs-*.bib and nowhere else.
    assert os.path.isfile(os.path.join(root, "writeup", "references.bib"))
    assert os.path.isfile(os.path.join(paths["thesis"], "_ch01-refs-2026-09-23.bib"))
    # A chapter writes figures/F1.1-x.png, so the figure must be at that path.
    assert os.path.isfile(os.path.join(paths["figures"], "F1.1-x.png"))
    # A figure subpath survives: the thesis references figures/F6.2-src/F6.2-v4.svg.
    assert os.path.isfile(os.path.join(paths["figures"], "F6.2-src", "F6.2-v4.svg"))
    assert os.path.isfile(os.path.join(paths["inserted"], "ch02-paper.pdf"))
    # The staging copy is gone, so nothing is served twice from one workspace.
    assert not os.path.exists(os.path.join(root, "_archive"))
    assert mapping["ignored"] == []


def test_chapters_are_detected_by_prefix(tmp_path, profile):
    entries = dict(MINIMAL)
    entries["src/04-nf-nomad.md"] = "# nf-nomad\n"
    entries["src/09-appendices.md"] = "# Appendices\n"
    paths, _, _ = workspace.create_from_archive(
        str(tmp_path / "ws"), _zip(tmp_path, entries))
    assert workspace.detected_chapters(paths) == ["01", "04", "09"]


def test_a_wrapping_directory_is_stripped(tmp_path, profile):
    """Zipping a folder yields thesis/src/..., which must work the same."""
    entries = {f"thesis/{k}": v for k, v in MINIMAL.items()}
    paths, _, manifest = workspace.create_from_archive(
        str(tmp_path / "ws"), _zip(tmp_path, entries))
    assert manifest["stripped_prefix"] == "thesis"
    assert os.path.isfile(os.path.join(paths["thesis"], "01-introduction.md"))


def test_an_unreadable_bibliography_name_is_refused_not_misplaced(tmp_path, profile):
    """The pipeline reads references.bib and _*-refs-*.bib. Nothing else.

    Placing `extra.bib` anywhere would lose every citation it carries without an
    error, so the archive is refused and the message names the two accepted shapes.
    """
    entries = dict(MINIMAL)
    entries["src/extra.bib"] = "@article{c}\n"
    with pytest.raises(workspace.BundleError) as e:
        workspace.create_from_archive(str(tmp_path / "ws"), _zip(tmp_path, entries))
    assert "references.bib" in str(e.value) and "_<area>-refs-" in str(e.value)


def test_a_nested_source_is_refused(tmp_path, profile):
    """writeup/thesis is flat, so a nested source would never be picked up."""
    entries = dict(MINIMAL)
    entries["src/parts/02-magma.md"] = "# MAGMA\n"
    with pytest.raises(workspace.BundleError) as e:
        workspace.create_from_archive(str(tmp_path / "ws"), _zip(tmp_path, entries))
    assert "directly in src/" in str(e.value)


def test_non_current_assets_are_reported_rather_than_built_with(tmp_path, profile):
    """archive/ and raw/ are the garden template's own words for "not the live set"."""
    entries = dict(MINIMAL)
    entries["assets/figures/archive/F1.1-old.png"] = b"\x89PNG\r\n"
    entries["assets/figures/raw/F1.1-x.ai"] = b"raw"
    entries["assets/supplemental-data/current/table.csv"] = "a,b\n"
    paths, mapping, _ = workspace.create_from_archive(
        str(tmp_path / "ws"), _zip(tmp_path, entries))
    assert not os.path.exists(os.path.join(paths["figures"], "F1.1-old.png"))
    assert mapping["ignored"] == [
        "assets/figures/archive/F1.1-old.png",
        "assets/figures/raw/F1.1-x.ai",
        "assets/supplemental-data/current/table.csv",
    ]


def test_an_archive_with_no_sources_is_refused(tmp_path, profile):
    entries = {"assets/figures/current/F1.1-x.png": b"\x89PNG\r\n"}
    with pytest.raises(workspace.BundleError) as e:
        workspace.create_from_archive(str(tmp_path / "ws"), _zip(tmp_path, entries))
    assert "no sources under src/" in str(e.value)


@pytest.mark.parametrize("bad", ["../../pwned", "/etc/passwd", "src/../../x.md"])
def test_traversal_is_refused_on_this_arm_too(tmp_path, profile, bad):
    """The guard lives in archive.extract, so it must still fire through this path."""
    from abcprint import archive
    entries = dict(MINIMAL)
    entries[bad] = "x"
    with pytest.raises(archive.ArchiveError):
        workspace.create_from_archive(str(tmp_path / "ws"), _zip(tmp_path, entries))


def test_the_manifest_reports_where_things_went(tmp_path, profile):
    _, mapping, manifest = workspace.create_from_archive(
        str(tmp_path / "ws"), _zip(tmp_path, MINIMAL))
    info = workspace.describe_thesis(manifest, mapping)
    assert info["layout"] == "project-garden"
    assert info["placed"] == {"sources": 2, "bibliography": 2, "figures": 2, "inserted": 1}
    assert "writeup/references.bib" in info["bibliography"]
