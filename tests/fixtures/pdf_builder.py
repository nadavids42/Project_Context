"""A minimal, dependency-free PDF builder for test fixtures.

Produces small, valid, multi-page PDFs with plain Helvetica text (or no
text at all, for the "scanned/empty" fixture) without needing a
PDF-writing library or a real Office/Adobe-produced binary committed to
the repo.
"""

from __future__ import annotations

import io


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_minimal_pdf(pages_text: list[str]) -> bytes:
    """Build a minimal valid PDF with one page per string in
    `pages_text`. An empty string produces a page with no text content
    at all (used for the scanned/empty-PDF fixture)."""
    n_pages = len(pages_text)
    catalog_num = 1
    pages_num = 2
    font_num = 3
    first_page_num = 4
    first_content_num = first_page_num + n_pages

    page_nums = [first_page_num + i for i in range(n_pages)]
    content_nums = [first_content_num + i for i in range(n_pages)]

    catalog_obj = f"<< /Type /Catalog /Pages {pages_num} 0 R >>".encode()
    kids = " ".join(f"{p} 0 R" for p in page_nums)
    pages_obj = f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode()
    font_obj = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    page_objs = [
        (
            f"<< /Type /Page /Parent {pages_num} 0 R "
            f"/Resources << /Font << /F1 {font_num} 0 R >> >> "
            f"/MediaBox [0 0 612 792] /Contents {c_num} 0 R >>"
        ).encode()
        for c_num in content_nums
    ]

    content_objs = []
    for text in pages_text:
        lines = text.split("\n") if text else []
        parts = ["BT", "/F1 12 Tf"]
        for i, line in enumerate(lines):
            parts.append("72 720 Td" if i == 0 else "0 -14 Td")
            parts.append(f"({_escape_pdf_text(line)}) Tj")
        parts.append("ET")
        content_objs.append("\n".join(parts).encode())

    obj_bodies: dict[int, bytes] = {
        catalog_num: catalog_obj,
        pages_num: pages_obj,
        font_num: font_obj,
    }
    for p_num, body in zip(page_nums, page_objs, strict=True):
        obj_bodies[p_num] = body
    for c_num, body in zip(content_nums, content_objs, strict=True):
        obj_bodies[c_num] = b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream"

    max_obj = max(obj_bodies)
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in range(1, max_obj + 1):
        offsets[num] = buf.tell()
        buf.write(f"{num} 0 obj\n".encode())
        buf.write(obj_bodies[num])
        buf.write(b"\nendobj\n")

    xref_offset = buf.tell()
    buf.write(f"xref\n0 {max_obj + 1}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for num in range(1, max_obj + 1):
        buf.write(f"{offsets[num]:010d} 00000 n \n".encode())
    buf.write(b"trailer\n")
    buf.write(f"<< /Size {max_obj + 1} /Root {catalog_num} 0 R >>\n".encode())
    buf.write(b"startxref\n")
    buf.write(f"{xref_offset}\n".encode())
    buf.write(b"%%EOF")
    return buf.getvalue()
