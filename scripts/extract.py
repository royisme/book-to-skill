#!/usr/bin/env python3
"""
Extract text from a PDF or EPUB file for book-to-skill processing.

PDF extraction tries methods in order:
  1. pdftotext (poppler-utils) — best quality
  2. PyPDF2 — common Python library
  3. pdfminer.six — thorough fallback

EPUB extraction tries methods in order:
  1. ebooklib + BeautifulSoup4 — best quality
  2. zipfile + html.parser — stdlib fallback (no extra deps)

Outputs:
  /tmp/book_skill_work/full_text.txt  — full extracted text
  /tmp/book_skill_work/metadata.json  — stats and metadata
"""

import html
import html.parser
import json
import os
import os.path
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

OUTPUT_DIR = Path("/tmp/book_skill_work")
OUTPUT_TEXT = OUTPUT_DIR / "full_text.txt"
OUTPUT_META = OUTPUT_DIR / "metadata.json"

CHAPTER_OVERSIZED_CHARS = 80_000  # ~20K tokens; flag for sub-agent splitting


def estimate_tokens(text: str) -> int:
    """Token count estimator. Tries tiktoken first, falls back to chars/4."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text, disallowed_special=()))
    except ImportError:
        # 4 chars/token works across English prose, code, and CJK
        return max(1, len(text) // 4)
    except Exception:
        return max(1, len(text) // 4)


def _epub_meta_from_opf(opf_text: str) -> dict:
    """Extract title and author from raw OPF XML text via regex."""
    title_m = re.search(r'<dc:title[^>]*>([^<]+)</dc:title>', opf_text, re.IGNORECASE)
    author_m = re.search(r'<dc:creator[^>]*>([^<]+)</dc:creator>', opf_text, re.IGNORECASE)
    return {
        "title": html.unescape(title_m.group(1).strip()) if title_m else None,
        "author": html.unescape(author_m.group(1).strip()) if author_m else None,
    }


def _extract_html_heading(raw_html: str) -> str | None:
    """Return the first h1/h2/h3 or <title> text from an HTML document."""
    for pattern in (
        r'<h[1-3][^>]*>(.*?)</h[1-3]>',
        r'<title[^>]*>(.*?)</title>',
    ):
        m = re.search(pattern, raw_html, re.IGNORECASE | re.DOTALL)
        if m:
            inner = re.sub(r'<[^>]+>', '', m.group(1))
            text = html.unescape(inner).strip()
            if text:
                return text
    return None


def extract_with_pdftotext(pdf_path: str) -> str | None:
    if not shutil.which("pdftotext"):
        return None
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", pdf_path, "-"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    except Exception:
        pass
    return None


def extract_with_pypdf2(pdf_path: str) -> str | None:
    try:
        import PyPDF2
        text_parts = []
        with open(pdf_path, "rb") as f:
            reader = PyPDF2.PdfReader(f)
            for page in reader.pages:
                try:
                    text_parts.append(page.extract_text() or "")
                except Exception:
                    text_parts.append("")
        return "\n".join(text_parts)
    except ImportError:
        return None
    except Exception:
        return None


def extract_with_pdfminer(pdf_path: str) -> str | None:
    try:
        from pdfminer.high_level import extract_text
        return extract_text(pdf_path)
    except ImportError:
        return None
    except Exception:
        return None


def extract_with_ebooklib(epub_path: str) -> tuple[str, list[dict], dict] | None:
    """Return (text, spine_chapters, epub_meta) or None."""
    try:
        import ebooklib
        from ebooklib import epub
        from bs4 import BeautifulSoup

        book = epub.read_epub(epub_path)

        title_list = book.get_metadata('DC', 'title')
        author_list = book.get_metadata('DC', 'creator')
        epub_meta = {
            "title": title_list[0][0] if title_list else None,
            "author": author_list[0][0] if author_list else None,
        }

        # Use spine order, not arbitrary item order
        spine_ids = [sid for (sid, _) in book.spine]
        items = {
            item.get_id(): item
            for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)
        }

        parts: list[str] = []
        spine_chapters: list[dict] = []
        pos = 0
        for spine_id in spine_ids:
            item = items.get(spine_id)
            if item is None:
                continue
            soup = BeautifulSoup(item.get_content(), "html.parser")
            heading_tag = soup.find(['h1', 'h2', 'h3'])
            heading = heading_tag.get_text().strip() if heading_tag else item.get_name()
            part = soup.get_text(separator="\n")
            if not part.strip():
                continue
            spine_chapters.append({"title": heading, "offset": pos})
            parts.append(part)
            pos += len(part) + 2  # +2 for "\n\n" separator

        if not parts:
            return None
        text = "\n\n".join(parts)
        for i, ch in enumerate(spine_chapters):
            ch["end_offset"] = (
                spine_chapters[i + 1]["offset"] if i + 1 < len(spine_chapters) else len(text)
            )
            ch["char_count"] = ch["end_offset"] - ch["offset"]
        _flag_oversized(spine_chapters)
        return text, spine_chapters, epub_meta
    except ImportError:
        return None
    except Exception:
        return None


class _HTMLTextExtractor(html.parser.HTMLParser):
    """Minimal HTML → plain text converter using stdlib only."""

    SKIP_TAGS = {"script", "style", "head"}

    def __init__(self):
        super().__init__()
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag in ("p", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "div"):
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth:
            self._parts.append(data)

    def get_text(self) -> str:
        return html.unescape("".join(self._parts))


def extract_with_zipfile(epub_path: str) -> tuple[str, list[dict], dict] | None:
    """stdlib-only EPUB extractor. Returns (text, spine_chapters, epub_meta) or None."""
    try:
        import xml.etree.ElementTree as ET
        with zipfile.ZipFile(epub_path) as zf:
            names = set(zf.namelist())
            # Find the OPF (rootfile) via container.xml; fall back to *.opf scan
            opf_path: str | None = None
            try:
                container = zf.read("META-INF/container.xml").decode(
                    "utf-8", errors="replace"
                )
                m = re.search(r'full-path=["\']([^"\']+)["\']', container)
                if m:
                    opf_path = m.group(1)
            except KeyError:
                pass
            if not opf_path:
                opf_candidates = [n for n in names if n.endswith(".opf")]
                if not opf_candidates:
                    return None
                opf_path = opf_candidates[0]

            opf_dir = os.path.dirname(opf_path)
            opf_text = zf.read(opf_path).decode("utf-8", errors="replace")
            epub_meta = _epub_meta_from_opf(opf_text)

            # Strip XML namespaces to make ElementTree queries simple
            opf_text_clean = re.sub(r'\sxmlns(:[^=]+)?="[^"]+"', "", opf_text, count=0)
            try:
                root = ET.fromstring(opf_text_clean)
            except ET.ParseError:
                return None

            # Build manifest: id -> href
            manifest: dict[str, str] = {}
            for item in root.findall(".//manifest/item"):
                item_id = item.get("id")
                href = item.get("href")
                if item_id and href:
                    manifest[item_id] = href

            # Resolve spine in reading order
            spine_files: list[str] = []
            for itemref in root.findall(".//spine/itemref"):
                idref = itemref.get("idref")
                if not idref or idref not in manifest:
                    continue
                href = manifest[idref]
                full = os.path.normpath(
                    os.path.join(opf_dir, href) if opf_dir else href
                ).replace("\\", "/")
                if full in names:
                    spine_files.append(full)

            # Fall back: any html/xhtml under the archive
            if not spine_files:
                spine_files = sorted(
                    n for n in names if n.lower().endswith((".html", ".xhtml"))
                )
            if not spine_files:
                return None

            parts: list[str] = []
            spine_chapters: list[dict] = []
            pos = 0
            for name in spine_files:
                try:
                    raw = zf.read(name).decode("utf-8", errors="replace")
                    heading = _extract_html_heading(raw)
                    parser = _HTMLTextExtractor()
                    parser.feed(raw)
                    part = parser.get_text()
                    if not part.strip():
                        continue
                    spine_chapters.append({
                        "title": heading or os.path.basename(name),
                        "offset": pos,
                    })
                    parts.append(part)
                    pos += len(part) + 2  # +2 for "\n\n" separator
                except Exception:
                    continue

            if not parts:
                return None
            text = "\n\n".join(parts)
            for i, ch in enumerate(spine_chapters):
                ch["end_offset"] = (
                    spine_chapters[i + 1]["offset"] if i + 1 < len(spine_chapters) else len(text)
                )
                ch["char_count"] = ch["end_offset"] - ch["offset"]
            _flag_oversized(spine_chapters)
            return text, spine_chapters, epub_meta
    except Exception:
        return None


def extract_epub(epub_path: str) -> tuple[str, str, list[dict], dict]:
    """Return (text, method, spine_chapters, epub_meta)."""
    print("Trying ebooklib + BeautifulSoup4...", end=" ", flush=True)
    result = extract_with_ebooklib(epub_path)
    if result:
        text, spine_chapters, epub_meta = result
        if text.strip():
            print("OK")
            return text, "ebooklib", spine_chapters, epub_meta

    print("not available")
    print("Trying stdlib zipfile parser...", end=" ", flush=True)
    result = extract_with_zipfile(epub_path)
    if result:
        text, spine_chapters, epub_meta = result
        if text.strip():
            print("OK")
            return text, "zipfile", spine_chapters, epub_meta

    print("FAILED")
    print(
        "\nERROR: Could not extract text from EPUB.\n"
        "Install ebooklib + beautifulsoup4 for best results:\n"
        "  pip3 install ebooklib beautifulsoup4",
        file=sys.stderr,
    )
    sys.exit(1)


def _pdfinfo_fields(pdf_path: str) -> dict[str, str]:
    """Run pdfinfo once and return all fields as a lowercase-keyed dict."""
    if not shutil.which("pdfinfo"):
        return {}
    try:
        result = subprocess.run(
            ["pdfinfo", pdf_path], capture_output=True, text=True, timeout=15
        )
        fields: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if ":" in line:
                key, _, val = line.partition(":")
                fields[key.strip().lower()] = val.strip()
        return fields
    except Exception:
        return {}


def count_pages(
    pdf_path: str,
    extracted_text: str | None = None,
    _info: dict | None = None,
) -> int:
    """Count pages, preferring pdfinfo, then form-feed chars, then PyPDF2."""
    info = _info if _info is not None else _pdfinfo_fields(pdf_path)
    if "pages" in info:
        try:
            return int(info["pages"])
        except ValueError:
            pass
    # pdftotext (default and -layout) emits \f between pages
    if extracted_text and "\f" in extracted_text:
        return extracted_text.count("\f") + 1
    try:
        import PyPDF2
        with open(pdf_path, "rb") as f:
            return len(PyPDF2.PdfReader(f).pages)
    except Exception:
        return 0


def _flag_oversized(chapters: list[dict]) -> None:
    """Mark chapters exceeding CHAPTER_OVERSIZED_CHARS with oversized=True."""
    for ch in chapters:
        if ch.get("char_count", 0) > CHAPTER_OVERSIZED_CHARS:
            ch["oversized"] = True


CHAPTER_PATTERN = re.compile(
    r"^\s*("
    r"chapter\s+\d+(?:[\.:]|\s|$)"
    r"|chapter\s+[ivxlcdm]+(?:[\.:]|\s|$)"
    r"|ch\.\s*\d+\b"
    r"|part\s+(?:\d+|[ivxlcdm]+)(?:[\.:]|\s|$)"
    r"|第[\d一二三四五六七八九十百千零]+[章篇部回節节]"
    r")",
    re.IGNORECASE | re.MULTILINE,
)


def find_chapter_boundaries(text: str) -> tuple[list[dict], dict]:
    """Scan text once; return (boundaries, structure_info).

    Replaces the former separate detect_structure + find_chapter_boundaries calls.
    """
    matches = list(CHAPTER_PATTERN.finditer(text))
    boundaries: list[dict] = []
    headings_sample: list[str] = []
    for m in matches:
        line_end = text.find("\n", m.start())
        if line_end == -1:
            line_end = m.start() + 120
        title = text[m.start():line_end].strip()
        if len(headings_sample) < 10:
            headings_sample.append(title)
        boundaries.append({"title": title, "offset": m.start()})
    for i, b in enumerate(boundaries):
        b["end_offset"] = (
            boundaries[i + 1]["offset"] if i + 1 < len(boundaries) else len(text)
        )
        b["char_count"] = b["end_offset"] - b["offset"]
    _flag_oversized(boundaries)
    toc_keywords = ["table of contents", "contents", "目录", "目錄", "índice", "sumário"]
    has_toc = any(kw in text[:5000].lower() for kw in toc_keywords)
    structure = {
        "chapters_detected": len(boundaries),
        "chapter_headings_sample": headings_sample,
        "has_toc": has_toc,
    }
    return boundaries, structure


def _is_epub_zip(path: str) -> bool:
    """Per EPUB spec, the first entry must be uncompressed 'mimetype' file
    containing exactly 'application/epub+zip'."""
    try:
        with zipfile.ZipFile(path) as zf:
            mimetype = zf.read("mimetype").decode("ascii", errors="replace").strip()
            return mimetype == "application/epub+zip"
    except (zipfile.BadZipFile, KeyError):
        return False


def extract_with_docling(pdf_path: str) -> str | None:
    """Layout-aware extraction using Docling. Best for technical books with tables and code."""
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import PdfFormatOption

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = False
        pipeline_options.do_table_structure = True

        converter = DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)
            }
        )
        result = converter.convert(pdf_path)
        return result.document.export_to_markdown()
    except ImportError:
        return None
    except Exception:
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: extract.py <path-to-pdf-or-epub> [--mode technical|text]", file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]

    # Parse --mode flag
    extraction_mode = "text"
    if "--mode" in sys.argv:
        idx = sys.argv.index("--mode")
        if idx + 1 < len(sys.argv):
            extraction_mode = sys.argv[idx + 1].lower()
    if extraction_mode not in ("technical", "text"):
        extraction_mode = "text"
    requested_mode = extraction_mode

    if not os.path.exists(input_path):
        print(f"ERROR: File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    ext = Path(input_path).suffix.lower()
    is_epub = ext == ".epub"
    is_pdf = ext == ".pdf"

    if not is_epub and not is_pdf:
        # Sniff magic bytes as fallback
        with open(input_path, "rb") as f:
            header = f.read(8)
        if header[:4] == b"%PDF":
            is_pdf = True
        elif header[:2] == b"PK" and _is_epub_zip(input_path):
            is_epub = True
        else:
            print(
                "ERROR: Unsupported format. Detected ZIP but not EPUB.\n"
                "Supported: .pdf, .epub",
                file=sys.stderr,
            )
            sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    epub_meta: dict = {}
    pdf_info: dict = {}

    if is_epub:
        print(f"Extracting EPUB: {input_path}")
        text, method, chapters, epub_meta = extract_epub(input_path)
        pages = len(chapters)
        pages_label = "spine_items"
        toc_keywords = ["table of contents", "contents", "目录", "目錄", "índice", "sumário"]
        has_toc = any(kw in text[:5000].lower() for kw in toc_keywords)
        structure = {
            "chapters_detected": len(chapters),
            "chapter_headings_sample": [c["title"] for c in chapters[:10]],
            "has_toc": has_toc,
        }
    else:
        text: str | None = None
        print(f"Extracting PDF: {input_path}")
        if extraction_mode == "technical":
            print("Mode: technical — using Docling (layout-aware)...", end=" ", flush=True)
            text = extract_with_docling(input_path)
            if text:
                method = "docling"
                print("OK")
            else:
                print("not available, falling back to pdftotext")
                extraction_mode = "text"
                print(
                    "\n⚠️  WARNING: Technical mode requested but Docling not available.\n"
                    "   Falling back to text mode — tables and code blocks will be flattened.\n"
                    "   Install with: pip3 install docling\n",
                    file=sys.stderr,
                )

        if extraction_mode == "text":
            print("Mode: text — using pdftotext...")
            print("Trying pdftotext...", end=" ", flush=True)
            text = extract_with_pdftotext(input_path)

            if text:
                method = "pdftotext"
                print("OK")
            else:
                print("not available")
                print("Trying PyPDF2...", end=" ", flush=True)
                text = extract_with_pypdf2(input_path)
                if text:
                    method = "PyPDF2"
                    print("OK")
                else:
                    print("not available")
                    print("Trying pdfminer.six...", end=" ", flush=True)
                    text = extract_with_pdfminer(input_path)
                    if text:
                        method = "pdfminer"
                        print("OK")
                    else:
                        print("FAILED")
                        print(
                            "\nERROR: Could not extract text from PDF.\n"
                            "Install one of: poppler-utils (pdftotext), PyPDF2, or pdfminer.six\n"
                            "  sudo apt install poppler-utils\n"
                            "  pip3 install PyPDF2\n"
                            "  pip3 install pdfminer.six",
                            file=sys.stderr,
                        )
                        sys.exit(1)

        assert text is not None
        pdf_info = _pdfinfo_fields(input_path)
        pages = count_pages(input_path, extracted_text=text, _info=pdf_info)
        pages_label = "pages"
        chapters, structure = find_chapter_boundaries(text)

    # Write full text
    OUTPUT_TEXT.write_text(text, encoding="utf-8")

    tokens = estimate_tokens(text)
    file_size_mb = os.path.getsize(input_path) / (1024 * 1024)

    metadata: dict = {
        "source_file": str(Path(input_path).resolve()),
        "filename": Path(input_path).name,
        "format": "epub" if is_epub else "pdf",
        "extraction_method": method,
        "file_size_mb": round(file_size_mb, 2),
        pages_label: pages,
        "chars": len(text),
        "words": len(text.split()),
        "estimated_tokens": tokens,
        "estimated_tokens_human": f"~{tokens // 1000}K",
        "output_text": str(OUTPUT_TEXT),
        **structure,
        "chapters": chapters,
    }

    # Mode fields only apply to PDF (EPUB extraction has no mode selection)
    if not is_epub:
        metadata["extraction_mode_requested"] = requested_mode
        metadata["extraction_mode_used"] = extraction_mode
        # Title/author from pdfinfo when available
        pdf_title = pdf_info.get("title") or None
        pdf_author = pdf_info.get("author") or None
        if pdf_title:
            metadata["title"] = pdf_title
        if pdf_author:
            metadata["author"] = pdf_author

    # Merge EPUB-sourced title/author when present
    if is_epub:
        for key in ("title", "author"):
            if epub_meta.get(key):
                metadata[key] = epub_meta[key]

    OUTPUT_META.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))

    page_line = f"   {'Spine items' if is_epub else 'Pages'}: {pages}"
    print("\n📖 Extraction complete:")
    print(f"   Format  : {'EPUB' if is_epub else 'PDF'}")
    print(f"   Method  : {method}")
    book_title = epub_meta.get("title") if is_epub else pdf_info.get("title")
    book_author = epub_meta.get("author") if is_epub else pdf_info.get("author")
    if book_title:
        print(f"   Title   : {book_title}")
    if book_author:
        print(f"   Author  : {book_author}")
    print(page_line)
    print(f"   Words   : {len(text.split()):,}")
    print(f"   Tokens  : ~{tokens // 1000}K")
    print(f"   Chapters: {structure['chapters_detected']} detected")
    print(f"   ToC     : {'yes' if structure['has_toc'] else 'not detected'}")
    print(f"\n   Text → {OUTPUT_TEXT}")
    print(f"   Meta → {OUTPUT_META}")


if __name__ == "__main__":
    main()
