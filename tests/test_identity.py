"""The title page's identity is refused loudly or accepted; it is never half-applied.

Every field here has a fallback inside the typst template. That makes a DROPPED key
the dangerous case: it produces neither an error nor a blank, but the template's own
value — which is how a real dissertation came to render with someone else's name,
department and supervisor on the title page, pass all 22 compliance checks and
report `compliant: true`. So anything not understood is refused by name.
"""
import pytest

from abcprint import identity
from abcprint.identity import IdentityError


def test_a_full_identity_is_accepted():
    fields = identity.parse(
        "author: R. E. Searcher\n"
        "degree: Doctor of Philosophy\n"
        "department: Department of Invented Examples\n"
        "faculty: Faculty of Medicine and Health Sciences\n"
        "supervisor: Prof. D. Emonstration\n"
        "award-date: March 2027\n"
        "papers-published: two\n"
        "papers-unpublished: three\n"
        'copyright-year: "2026"\n')
    assert set(fields) == set(identity.KEYS) - {"declaration-date"}
    assert fields["author"] == "R. E. Searcher"
    # A year written unquoted in YAML is an int; it still reaches typst as text.
    assert identity.parse("copyright-year: 2026\n")["copyright-year"] == "2026"


def test_a_partial_identity_is_fine():
    """Absent keys fall back to the template, so supplying two is legitimate."""
    assert identity.parse("author: A\nsupervisor: B\n") == {"author": "A", "supervisor": "B"}


def test_an_empty_file_is_fine():
    assert identity.parse("") == {}
    assert identity.parse("# only a comment\n") == {}


def test_a_misspelled_key_is_refused_by_name():
    """The case that matters: `supervsor` dropped leaves the template's supervisor."""
    with pytest.raises(IdentityError) as e:
        identity.parse("author: A\nsupervsor: Prof Typo\n")
    assert "supervsor" in str(e.value) and "silently" in str(e.value)


def test_a_key_the_template_does_not_read_is_refused():
    """`title` is set by the build, not here; accepting it would do nothing."""
    with pytest.raises(IdentityError) as e:
        identity.parse("title: My Thesis\n")
    assert "title" in str(e.value)


@pytest.mark.parametrize("body,why", [
    ("author:\n  - A\n  - B\n", "not a single value"),
    ("author:\n  name: A\n", "not a single value"),
    ("author: |\n  A\n  B\n", "spans several lines"),
    ('author: "A\\nB"\n', "spans several lines"),
    ("author: " + "x" * 400 + "\n", "over the length limit"),
])
def test_a_value_that_cannot_be_rendered_is_refused(body, why):
    with pytest.raises(IdentityError):
        identity.parse(body)


@pytest.mark.parametrize("ch", ['"', "\\"])
def test_a_character_the_generated_typst_cannot_escape_is_refused(ch):
    """The author reaches a typst STRING literal, and a template cannot escape one.

    set document(author: …) takes a string, not content, so the author is emitted as
    "…" in generated typst. A quote or backslash there is a syntax error several
    steps away from whoever typed it.
    """
    with pytest.raises(IdentityError) as e:
        identity.parse(f"author: Ab{ch}hinav\n")
    assert "escape" in str(e.value)


def test_a_non_mapping_is_refused():
    with pytest.raises(IdentityError):
        identity.parse("- author\n")


def test_broken_yaml_is_refused_as_yaml():
    with pytest.raises(IdentityError) as e:
        identity.parse("author: [unclosed\n")
    assert "not valid YAML" in str(e.value)


def test_the_service_and_the_pipeline_allow_the_same_keys():
    """Two copies of one list drift. If they do, a key is accepted here and refused
    mid-render — or worse, accepted by the pipeline and never validated."""
    import pathlib
    import re
    src = pathlib.Path("pipeline/thesis-su-pdf.sh")
    if not src.is_file():          # image built without the thesis pipeline
        pytest.skip("pipeline not vendored")
    m = re.search(r"ALLOWED = \((.*?)\)", src.read_text(), re.S)
    assert m, "ALLOWED not found in the pipeline script"
    assert set(re.findall(r'"([a-z-]+)"', m.group(1))) == set(identity.KEYS)
