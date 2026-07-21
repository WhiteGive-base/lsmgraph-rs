#!/usr/bin/env python3
"""Rasterize figure PDFs and check page count, margins, and file integrity."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageChops


DEFAULT_PDFINFO = Path(
    r"C:\Users\Coword\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin\pdfinfo.exe"
)
DEFAULT_PDFTOPPM = Path(
    r"C:\Users\Coword\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\poppler\Library\bin\pdftoppm.exe"
)


def pdf_pages(pdfinfo: Path, path: Path) -> int:
    result = subprocess.run(
        [str(pdfinfo), str(path)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    match = re.search(r"^Pages:\s+(\d+)$", result.stdout, flags=re.MULTILINE)
    if not match:
        raise RuntimeError(f"Could not read page count from {path}")
    return int(match.group(1))


def ink_margins(path: Path) -> dict[str, int | list[int]]:
    with Image.open(path).convert("RGB") as image:
        white = Image.new("RGB", image.size, "white")
        difference = ImageChops.difference(image, white)
        bbox = difference.getbbox()
        if bbox is None:
            raise RuntimeError(f"Rendered page is blank: {path}")
        left, top, right, bottom = bbox
        return {
            "size_px": [image.width, image.height],
            "left": left,
            "top": top,
            "right": image.width - right,
            "bottom": image.height - bottom,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--render-dir", type=Path, required=True)
    parser.add_argument("--pdfinfo", type=Path, default=DEFAULT_PDFINFO)
    parser.add_argument("--pdftoppm", type=Path, default=DEFAULT_PDFTOPPM)
    parser.add_argument("--min-margin-px", type=int, default=4)
    args = parser.parse_args()

    args.render_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    failures: list[str] = []
    pdfs = sorted(args.input_dir.glob("fig*.pdf"))
    if not pdfs:
        raise SystemExit(f"No figure PDFs found under {args.input_dir}")

    for pdf in pdfs:
        pages = pdf_pages(args.pdfinfo, pdf)
        prefix = args.render_dir / pdf.stem
        subprocess.run(
            [str(args.pdftoppm), "-png", "-r", "180", "-singlefile", str(pdf), str(prefix)],
            check=True,
            capture_output=True,
        )
        png = prefix.with_suffix(".png")
        margins = ink_margins(png)
        status = "PASS"
        if pages != 1:
            status = "FAIL"
            failures.append(f"{pdf.name}: expected 1 page, found {pages}")
        for side in ("left", "top", "right", "bottom"):
            if int(margins[side]) < args.min_margin_px:
                status = "FAIL"
                failures.append(f"{pdf.name}: {side} ink margin is {margins[side]} px")
        records.append(
            {
                "pdf": pdf.name,
                "rendered_png": str(png),
                "pages": pages,
                "ink_margins_px": margins,
                "status": status,
            }
        )

    report = {"figures": records, "failures": failures, "status": "PASS" if not failures else "FAIL"}
    report_path = args.render_dir / "render-validation.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(report_path)
    if failures:
        raise SystemExit("; ".join(failures))


if __name__ == "__main__":
    main()
