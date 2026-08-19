#!/usr/bin/env bash
set -euo pipefail

# Jupiter cloud math toolchain bootstrap.
# MiKTeX is preferred when explicitly requested; Ubuntu TeX Live is the
# default for CI because it is deterministic and readily available.

export DEBIAN_FRONTEND=noninteractive

sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
  latexmk \
  texlive-latex-base \
  texlive-latex-recommended \
  texlive-latex-extra \
  texlive-science \
  texlive-fonts-recommended \
  dvipng \
  dvisvgm

if command -v pdflatex >/dev/null 2>&1; then
  echo "LATEX_ENGINE=pdflatex" >> "${GITHUB_ENV:-/tmp/jupiter_math_env}"
  pdflatex --version | head -n 1
else
  echo "LaTeX engine installation failed" >&2
  exit 1
fi
