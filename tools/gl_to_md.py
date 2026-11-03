#!/usr/bin/env python3
"""Convert an MTNOS General Ledger Report PDF (or PNG/JPG screenshot) to Markdown.

Usage:
    python3 tools/gl_to_md.py report.pdf [more.pdf ...]          # print Markdown
    python3 tools/gl_to_md.py report.pdf --out-dir md/           # also write .md files

How it reads the report:
  1. If the PDF has a text layer, pdfplumber's table extraction is used.
  2. Otherwise each page is rendered at 300 dpi and OCR'd with tesseract.
     Table rows are found from the grey rule lines printed between rows, so a
     wrapped account name ("Web processing fee (Payment / Provider)") stays one row.
     Words are put in the Debit or Credit column by which header they line up under.

Every table is checked: the sum of the Debit and Credit rows must equal the
printed Total line. A failed check is printed as CHECK FAILED. OCR can misread
digits, so a failed check means read the page image yourself.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal, InvalidOperation

AMT_RE = re.compile(r"^\$?-?[\d,]+\.\d{2}$")
DATE_RANGE_RE = re.compile(r"(\d{2}/\d{2}/\d{4})\s*-\s*(\d{2}/\d{2}/\d{4})")
GEN_RE = re.compile(r"Report generated:\s*(.+)")


def money(s):
    if not s:
        return None
    try:
        return Decimal(s.replace("$", "").replace(",", ""))
    except InvalidOperation:
        return None


# ---------------------------------------------------------------- text layer
def text_layer(pdf):
    try:
        import pdfplumber
    except ImportError:
        return None
    pages = []
    with pdfplumber.open(pdf) as p:
        for pg in p.pages:
            txt = pg.extract_text() or ""
            if not txt.strip():
                return None
            rows = []
            for t in pg.extract_tables():
                for r in t:
                    cells = [(c or "").replace("\n", " ").strip() for c in r]
                    if cells and cells[0].lower() == "accounts":
                        continue
                    while len(cells) < 4:
                        cells.append("")
                    rows.append(cells[:4])
            pages.append((txt, rows))
    return pages


# ---------------------------------------------------------------- OCR path
def group_lines(words):
    """Cluster words into visual lines by vertical centre, then order left to right.
    (A dash sits lower than digits, so grouping on top edge would split '95 - 3047'.)"""
    lines = []
    for w in sorted(words, key=lambda w: (w["y0"] + w["y1"]) / 2):
        c = (w["y0"] + w["y1"]) / 2
        if lines and abs(c - lines[-1]["c"]) < 18:
            ln = lines[-1]
            ln["w"].append(w)
            ln["c"] = sum((x["y0"] + x["y1"]) / 2 for x in ln["w"]) / len(ln["w"])
        else:
            lines.append({"c": c, "w": [w]})
    return [sorted(ln["w"], key=lambda w: w["x0"]) for ln in lines]


def ocr_words(png):
    out = subprocess.run(["tesseract", png, "-", "--psm", "4", "tsv"],
                         capture_output=True, text=True, check=True).stdout
    words = []
    for line in out.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 12 or not f[11].strip():
            continue
        left, top, w, h, conf = map(float, f[6:11])
        words.append(dict(text=f[11].strip(), x0=left, x1=left + w,
                          y0=top, y1=top + h, conf=conf))
    return words


def rule_lines(png):
    """y positions of horizontal rules (rows where most pixels are non-white)."""
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(png).convert("L"))
    w = a.shape[1]
    dark = (a < 235).sum(axis=1) / w
    ys, prev = [], -10
    for y, frac in enumerate(dark):
        if frac > 0.6:
            if y - prev > 3:
                ys.append(y)
            prev = y
    return ys


def ocr_page(png):
    words = ocr_words(png)
    full = " ".join(w["text"] for w in words)
    text = "\n".join(" ".join(w["text"] for w in ln) for ln in group_lines(words))

    hdr = {k: next((w for w in words if w["text"].rstrip(".:") == k), None)
           for k in ("Accounts", "GL", "Debit", "Credit")}
    if not all(hdr.values()):
        return text, [], ["could not find the Accounts / GL No. / Debit / Credit header"]
    rules = [y for y in rule_lines(png) if y > hdr["Accounts"]["y0"] - 40]
    gl_x = hdr["GL"]["x0"] - 20
    deb_r, cred_r = hdr["Debit"]["x1"], hdr["Credit"]["x1"]
    amt_left = hdr["Debit"]["x0"] - 250

    rows, warnings = [], []
    for top, bot in zip(rules, rules[1:]):
        band = [w for w in words if top < (w["y0"] + w["y1"]) / 2 < bot]
        if not band or any(w is hdr["Accounts"] for w in band):
            continue
        acct, gl, deb, cred = [], [], [], []
        for w in (w for ln in group_lines(band) for w in ln):
            if w["x0"] < gl_x:
                acct.append(w["text"])
            elif AMT_RE.match(w["text"]) and w["x0"] > amt_left:
                (deb if abs(w["x1"] - deb_r) < abs(w["x1"] - cred_r) else cred).append(w["text"])
                if w["conf"] < 85:
                    warnings.append(f"low OCR confidence ({w['conf']:.0f}) on {w['text']}")
            else:
                gl.append(w["text"])
        rows.append([" ".join(acct), " ".join(gl), " ".join(deb), " ".join(cred)])
    return text, rows, warnings


def render(pdf, tmp):
    subprocess.run(["pdftoppm", "-png", "-r", "300", pdf, os.path.join(tmp, "p")], check=True)
    return sorted(os.path.join(tmp, f) for f in os.listdir(tmp) if f.endswith(".png"))


# ---------------------------------------------------------------- output
def to_markdown(path):
    method, texts, rows, warns = None, [], [], []
    if path.lower().endswith(".pdf"):
        tl = text_layer(path)
        if tl:
            method = "pdfplumber text layer"
            for txt, r in tl:
                texts.append(txt); rows += r
    if method is None:
        if not shutil.which("tesseract"):
            sys.exit("tesseract not installed: run tools/setup.sh")
        method = "OCR (tesseract, 300 dpi)"
        with tempfile.TemporaryDirectory() as tmp:
            pngs = render(path, tmp) if path.lower().endswith(".pdf") else [path]
            for png in pngs:
                t, r, w = ocr_page(png)
                texts.append(t); rows += r; warns += w

    text = "\n".join(texts)
    dr = DATE_RANGE_RE.search(text)
    gen = GEN_RE.search(text)

    body = [r for r in rows if not r[0].lower().startswith("total")]
    total = next((r for r in rows if r[0].lower().startswith("total")), None)
    sd = sum((money(r[2]) or 0 for r in body), Decimal("0"))
    sc = sum((money(r[3]) or 0 for r in body), Decimal("0"))

    md = [f"# {os.path.basename(path)}", ""]
    md.append(f"- **Entry date (DD/MM/YYYY):** {dr.group(1) + ' - ' + dr.group(2) if dr else 'NOT FOUND'}")
    md.append(f"- **Report generated:** {gen.group(1).strip() if gen else 'not found'}")
    md.append(f"- **Read with:** {method}")
    md += ["", "| Accounts | GL No. | Debit | Credit |", "|---|---|---:|---:|"]
    for r in rows:
        md.append("| " + " | ".join(c.replace("|", "/") for c in r) + " |")
    md.append("")
    if total:
        td, tc = money(total[2]), money(total[3])
        ok = td == sd and tc == sc and td == tc
        md.append(f"**{'CHECK OK' if ok else 'CHECK FAILED'}**: rows sum to Debit {sd:.2f} / Credit {sc:.2f}; "
                  f"printed Total is Debit {total[2] or '-'} / Credit {total[3] or '-'}.")
        if not ok:
            md.append("Read the page image yourself before building the entry.")
    else:
        md.append("**CHECK FAILED**: no Total line found. Read the page image yourself.")
    for w in warns:
        md.append(f"- Warning: {w}")
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out-dir")
    a = ap.parse_args()
    for f in a.files:
        md = to_markdown(f)
        print(md)
        if a.out_dir:
            os.makedirs(a.out_dir, exist_ok=True)
            with open(os.path.join(a.out_dir, os.path.splitext(os.path.basename(f))[0] + ".md"), "w") as fh:
                fh.write(md)


if __name__ == "__main__":
    main()
