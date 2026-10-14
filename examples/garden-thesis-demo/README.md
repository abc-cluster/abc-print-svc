# garden-thesis-demo — the thesis engine, from a zip

The same invented content as `thesis-chapter-demo`, rearranged into the
**project-garden** layout so the archive arm of the thesis engine can be
exercised without anything private:

    src/01-introduction.md      a chapter, selected by its NN- prefix
                                (its 0-EN / 0-AF sections become the abstract pages,
                                 and that is what attaches the front matter)
    src/thesis.yml              the title page's identity
    src/Dedication.md           front matter, read by name
    src/Acknowledgements.md
    src/references.bib          the main bibliography
    assets/figures/current/     figures, referenced from a chapter as figures/<name>

Build it:

    (cd examples/garden-thesis-demo && zip -qr ../garden-thesis-demo.zip .)
    curl -s -X POST localhost:8080/compile/archive \
      -F bundle=@examples/garden-thesis-demo.zip \
      -F profile=su-fmhs -F copy=examination

Unlike the quarto arm, this one does not preserve the archive's layout: the
pipeline reads fixed locations under `writeup/`, so the garden layout is mapped
into them. The job manifest reports where each file landed, and a file that
matches no mapped location is listed under `archive.ignored` rather than being
placed somewhere nothing reads.

Front matter attaches to the chapter carrying the abstracts, so a build of a
single non-introduction chapter legitimately shows the template's placeholder
pages. That is the pipeline's gating, not a mapping failure.

`src/thesis.yml` is the part most worth copying. Delete it and rebuild: the title
page renders `‹author - unset; supply it in thesis.yml›` and check **C1 fails**.
That is deliberate — a document that is wrong about whose it is must not be able
to render clean, which is exactly what it did until 2026-10-14.

Check `L3` fails on this fixture and on `thesis-chapter-demo` alike: it asserts a
full thesis's reference list (it treats fewer than 50 DOIs after the heading as a
truncated list), and a one-chapter demo cannot have one. Padding the fixture to
satisfy it would be writing to the test rather than to the document.
