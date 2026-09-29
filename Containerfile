# abc-print-svc — a self-contained document compilation and verification image.
#
# Everything the render path needs is baked in: quarto (which brings the typst
# that actually renders), pandoc, qpdf, poppler, python with a pypdf new enough
# to merge, the pipeline scripts, and a font set.
#
# On fonts. The SU/FMHS template names Calibri, Cambria, Georgia, Times New
# Roman, Arial and Helvetica Neue. All six are Microsoft's, Monotype's or
# Apple's and cannot be redistributed in an image, so what ships here is the
# metric-compatible libre set — Carlito, Caladea, Gelasio, Tinos, Arimo — which
# makes the image work out of the box without redistributing anything.
#
# That is a real substitution and it is declared, never silent: the faculty
# length rule is measured in PAGES, so pagination is load-bearing. An operator
# holding a licence that covers server-side rendering mounts the originals at
# /usr/local/share/fonts/licensed and they win automatically, because fontconfig
# resolves the real family name ahead of the stand-in.
FROM debian:bookworm-slim

# Link the published package to its repository. Without
# org.opencontainers.image.source a GHCR package is an orphan: it does not appear
# on the repo page and does not inherit repo permissions, so org members who can
# read the code still cannot pull the image.
LABEL org.opencontainers.image.source="https://github.com/abc-cluster/abc-print-svc" \
      org.opencontainers.image.description="Document compilation and compliance verification for thesis-style documents" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.title="abc-print-svc" \
      org.opencontainers.image.version="0.1.0"

ARG QUARTO_VERSION=1.7.31
ARG PANDOC_VERSION=3.7.0.1
ARG DEBIAN_FRONTEND=noninteractive

# --- system toolchain -------------------------------------------------------
# qpdf does the page surgery, poppler backs every regression check, fontconfig
# resolves families for typst.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl \
      qpdf poppler-utils \
      python3 python3-pip \
      fontconfig \
      zip unzip \
      # metric-compatible libre substitutes for the proprietary families:
      #   Carlito -> Calibri, Caladea -> Cambria,
      #   Tinos -> Times New Roman, Arimo -> Arial, Cousine -> Courier New
      fonts-crosextra-carlito fonts-crosextra-caladea fonts-croscore \
      fonts-dejavu \
    && rm -rf /var/lib/apt/lists/*

# --- quarto (brings the typst that renders) ---------------------------------
RUN curl -fsSL -o /tmp/quarto.deb \
      "https://github.com/quarto-dev/quarto-cli/releases/download/v${QUARTO_VERSION}/quarto-${QUARTO_VERSION}-linux-amd64.deb" \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/quarto.deb \
    && rm -f /tmp/quarto.deb && rm -rf /var/lib/apt/lists/*

# --- pandoc -----------------------------------------------------------------
# The pipeline shells out to `pandoc` directly for the DOCX path and the
# markdown slicing, so it must be on PATH in its own right rather than only
# inside quarto.
RUN curl -fsSL -o /tmp/pandoc.deb \
      "https://github.com/jgm/pandoc/releases/download/${PANDOC_VERSION}/pandoc-${PANDOC_VERSION}-1-amd64.deb" \
    && apt-get update && apt-get install -y --no-install-recommends /tmp/pandoc.deb \
    && rm -f /tmp/pandoc.deb && rm -rf /var/lib/apt/lists/*

# --- fonts that are genuinely free, shipped as themselves -------------------
# Gelasio (Georgia-metric-compatible) and JetBrains Mono are OFL, so unlike the
# five above these are the real thing rather than a stand-in.
#
# These downloads are FATAL on failure by design. An earlier revision swallowed
# errors with `|| echo NOTE`, and the image built "successfully" with Georgia
# missing — the same shape of defect as the qpdf merge fallback, where a warning
# nobody reads stands in for a failure. If a font we claim to ship is not here,
# the build stops.
RUN mkdir -p /usr/local/share/fonts/ofl/gelasio /usr/local/share/fonts/ofl/jetbrains \
      /usr/local/share/fonts/licensed \
 && curl -fsSL -o /usr/local/share/fonts/ofl/gelasio/Gelasio.ttf \
      "https://github.com/google/fonts/raw/main/ofl/gelasio/Gelasio%5Bwght%5D.ttf" \
 && curl -fsSL -o /usr/local/share/fonts/ofl/gelasio/Gelasio-Italic.ttf \
      "https://github.com/google/fonts/raw/main/ofl/gelasio/Gelasio-Italic%5Bwght%5D.ttf" \
 && curl -fsSL -o /tmp/monaspace.zip \
      "https://github.com/githubnext/monaspace/releases/download/v1.101/monaspace-v1.101.zip" \
 && unzip -q -o -j /tmp/monaspace.zip '*/otf/*.otf' -d /usr/local/share/fonts/ofl/monaspace \
 && rm -f /tmp/monaspace.zip \
 && curl -fsSL -o /tmp/jbm.zip \
      "https://github.com/JetBrains/JetBrainsMono/releases/download/v2.304/JetBrainsMono-2.304.zip" \
 && unzip -q -o -j /tmp/jbm.zip 'fonts/ttf/*' -d /usr/local/share/fonts/ofl/jetbrains \
 && rm -f /tmp/jbm.zip \
 && fc-cache -f \
 # Prove the families the profile depends on are actually resolvable, rather
 # than trusting that the files landed.
 && for fam in Carlito Caladea Gelasio Tinos Arimo "JetBrains Mono" "Monaspace Argon"; do \
      fc-list : family | tr ',' '\n' | grep -qxF "$fam" \
        || { echo "FATAL: font family '$fam' did not install"; exit 1; }; \
    done \
 && echo "font families verified"

# --- python -----------------------------------------------------------------
# pypdf is pinned above 3.9 because the merge needs PdfWriter(clone_from=...).
# An older pypdf imports cleanly and dies mid-merge; the qpdf fallback then
# rebuilds the document without its name tree and silently dangles every
# internal link. The health check probes the capability, not the version.
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && rm -f /tmp/requirements.txt

# --- API documentation assets ----------------------------------------------
# ReDoc and Swagger UI are vendored rather than pulled from a CDN at page load.
# The renderer is meant to run with no network egress, and documentation that
# blanks out in the deployment it documents is not documentation.
RUN mkdir -p /srv/vendor \
 && curl -fsSL -o /srv/vendor/redoc.standalone.js \
      "https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js" \
 && curl -fsSL -o /srv/vendor/swagger-ui-bundle.js \
      "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js" \
 && curl -fsSL -o /srv/vendor/swagger-ui.css \
      "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" \
 && for f in redoc.standalone.js swagger-ui-bundle.js swagger-ui.css; do \
      test -s "/srv/vendor/$f" || { echo "FATAL: doc asset $f did not download"; exit 1; }; \
    done \
 && echo "doc assets vendored"

# --- the service ------------------------------------------------------------
WORKDIR /srv
COPY src/ /srv/src/
COPY profiles/ /srv/profiles/
# Pipeline scripts, vendored into the build context by docker/vendor-pipeline.sh.
COPY pipeline/ /srv/pipeline/
ENV PYTHONPATH=/srv/src \
    ABCPRINT_VENDOR=/srv/vendor \
    ABCPRINT_FONT_PATHS=/usr/local/share/fonts/licensed:/usr/local/share/fonts/ofl \
    ABCPRINT_PIPELINE=/srv/pipeline \
    ABCPRINT_PROFILE=/srv/profiles/su-fmhs \
    ABCPRINT_PROFILES=/srv/profiles \
    ABCPRINT_DOWNLOADS=/srv/downloads

# Render unprivileged: arbitrary markdown is arbitrary input, and the service
# must not be a foothold. Network egress, read-only root and resource caps are
# applied at run time by the orchestrator, not here.
RUN useradd -m -u 10001 abcprint && mkdir -p /srv/work /srv/downloads \
    && chown -R abcprint /srv/work /srv/downloads
USER abcprint

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=15s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=10).status==200 else 1)"

CMD ["python3","-m","uvicorn","abcprint.api:app","--host","0.0.0.0","--port","8080"]
