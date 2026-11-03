#!/bin/bash
# Installs what tools/gl_to_md.py needs. Safe to run repeatedly.
# MTNOS General Ledger PDFs are image-only, so an OCR engine is required.
set -euo pipefail
cd "$(dirname "$0")/.."

missing=()
command -v tesseract >/dev/null || missing+=(tesseract-ocr)
command -v pdftoppm  >/dev/null || missing+=(poppler-utils)
if [ ${#missing[@]} -gt 0 ]; then
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q "${missing[@]}"
fi

python3 -m pip install -q --break-system-packages -r tools/requirements.txt
