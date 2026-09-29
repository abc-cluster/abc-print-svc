"""The copy a caller asks for decides the front matter, so the mapping is worth a guard.

Three copies of one profile: the examination copy carries neither the Afrikaans Opsomming
nor SU's branded title frame, the submission copy adds the Opsomming, and the branded frame
belongs to the library deposit alone. Getting that wrong sends an examiner a branded
document, or a deposit without one, and neither is visible until someone reads the PDF.
"""
import pytest

from abcprint import compile as compiler
from abcprint.compile import CompileError

PATHS = {"bin": "/nonexistent", "repo": "/nonexistent"}


@pytest.mark.parametrize("copy", ["examination", "submission", "library"])
def test_known_copies_reach_the_pipeline(copy):
    """A known copy gets past validation and fails only for the absent pipeline."""
    with pytest.raises(CompileError) as e:
        compiler.run(PATHS, None, copy=copy)
    assert "pipeline script missing" in str(e.value)


@pytest.mark.parametrize("copy", ["", "SUBMISSION", "final", "deposit", "examiner"])
def test_unknown_copies_are_refused_by_name(copy):
    """An unknown copy is refused for being unknown, not for the missing pipeline.

    The validation runs before the pipeline is looked for precisely so that a caller who
    mistypes is told so, rather than being told the image is broken. Note "examiner" is
    refused here: that is the pipeline's own flag spelling, not this API's.
    """
    with pytest.raises(CompileError) as e:
        compiler.run(PATHS, None, copy=copy)
    assert "unknown copy" in str(e.value)
