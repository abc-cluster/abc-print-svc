# Introduction

## 0-EN

The template lifts this section out of the body and sets it as the Abstract page,
which is also what attaches the front matter to this chapter: a build whose
chapters carry no `0-EN` renders the template's own placeholder pages instead, and
check C1 fails on them. It is here so the fixture demonstrates a complete front
matter rather than a half-built one.

Nothing below describes real work. This fixture exists so that the thesis engine
can be exercised end to end without a private repository: an invented chapter with
a figure, a table, a cross-reference to each and a citation, which are the
constructs that have actually broken thesis builds.

## 0-AF

Hierdie is 'n fiktiewe opsomming. Dit bestaan slegs sodat die Afrikaanse bladsy
gebou kan word; die inskrywingskopie laat dit weg.

This is a fixture chapter. It is not part of any thesis and describes nothing
real. It exists so that the `thesis-assemble` engine can be exercised without a
private repository.

## Why a fixture chapter is shaped like this

A one-paragraph chapter renders under almost any misconfiguration, which makes
it useless for catching defects. This chapter therefore carries the constructs
that have actually broken thesis builds: a figure with a label and a width, a
table with a caption and a label, a cross-reference to each, and a citation.

The verification suite checks, among other things, that figures are numbered per
chapter and contiguously, that the list of figures agrees with the body, that
every cross-reference resolves, and that a References section exists
[@fixture2020placeholder]. A chapter with no figures, tables or citations
exercises none of that.

It also checks that a figure's filename encodes the number it renders under, so
the placeholder here is named `F1.1-placeholder.svg` rather than something
arbitrary [@fixture2019method].

## A figure

@fig-fixture is a generated placeholder. It carries no data.

![A generated placeholder figure. The shapes are arbitrary and the figure exists
so that numbering, the list of figures, and a cross-reference are all
exercised.](figures/F1.1-placeholder.svg){#fig-fixture width=60%}

## A table

@tbl-fixture reports invented counts.

| Item   | Count | Notes    |
|--------|-------|----------|
| Alpha  | 12    | Invented |
| Beta   | 7     | Invented |
| Gamma  | 3     | Invented |

: Invented counts, present so that table numbering and the list of tables are
exercised. {#tbl-fixture}

## Closing

Nothing above is a finding. If this chapter builds and the checks pass, the
deployment can assemble a chapter, number a figure and a table, resolve
cross-references, and produce the lists that must agree with the body.
