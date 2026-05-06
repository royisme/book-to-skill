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

WORDS_PER_TOKEN = 0.75  # approximate


def estimate_tokens(text: str) -> int:
    return int(len(text.split()) / WORDS_PER_TOKEN)


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


def extract_with_ebooklib(epub_path: str) -> str | None:
    try:
        import ebooklib
        from ebooklib import epub
        from bs4 import BeautifulSoup

        book = epub.read_epub(epub_path)
        parts = []
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT):
            soup = BeautifulSoup(item.get_content(), "html.parser")
            parts.append(soup.get_text(separator="\n"))
        return "\n\n".join(parts)
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
        self._current_skip: str | None = None

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


def extract_with_zipfile(epub_path: str) -> str | None:
    """stdlib-only EPUB extractor: unzip → parse OPF spine → extract HTML."""
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
            for name in spine_files:
                try:
                    raw = zf.read(name).decode("utf-8", errors="replace")
                    parser = _HTMLTextExtractor()
                    parser.feed(raw)
                    parts.append(parser.get_text())
                except Exception:
                    continue
            return "\n\n".join(parts) if parts else None
    except Exception:
        return None


def extract_epub(epub_path: str) -> tuple[str, str]:
    """Return (text, method) for an EPUB file."""
    print("Trying ebooklib + BeautifulSoup4...", end=" ", flush=True)
    text = extract_with_ebooklib(epub_path)
    if text and text.strip():
        print("OK")
        return text, "ebooklib"

    print("not available")
    print("Trying stdlib zipfile parser...", end=" ", flush=True)
    text = extract_with_zipfile(epub_path)
    if text and text.strip():
        print("OK")
        return text, "zipfile"

    print("FAILED")
    print(
        "\nERROR: Could not extract text from EPUB.\n"
        "Install ebooklib + beautifulsoup4 for best results:\n"
        "  pip3 install ebooklib beautifulsoup4",
        file=sys.stderr,
    )
    sys.exit(1)


def count_epub_chapters(epub_path: str) -> int:
    """Count spine items (approximate chapter count) without dependencies."""
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_files = [n for n in zf.namelist() if n.endswith(".opf")]
            if not opf_files:
                return 0
            opf_text = zf.read(opf_files[0]).decode("utf-8", errors="replace")
            return len(re.findall(r'<itemref\b', opf_text))
    except Exception:
        return 0


def count_pages(pdf_path: str, extracted_text: str | None = None) -> int:
    """Count pages, preferring metadata sources, then form-feed in extracted text."""
    if shutil.which("pdfinfo"):
        try:
            result = subprocess.run(
                ["pdfinfo", pdf_path], capture_output=True, text=True, timeout=15
            )
            for line in result.stdout.splitlines():
                if line.startswith("Pages:"):
                    return int(line.split(":")[1].strip())
        except Exception:
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


def detect_structure(text: str) -> dict:
    """Detect chapter count and table of contents presence.
    Scans the entire text, not just the first 50K chars."""
    matches = list(CHAPTER_PATTERN.finditer(text))
    headings_sample = []
    for m in matches[:10]:
        line_end = text.find("\n", m.start())
        line = text[m.start(): line_end if line_end != -1 else m.start() + 120]
        headings_sample.append(line.strip())
    toc_keywords = ["table of contents", "contents", "目录", "目錄", "índice", "sumário"]
    has_toc = any(kw in text[:5000].lower() for kw in toc_keywords)
    return {
        "chapters_detected": len(matches),
        "chapter_headings_sample": headings_sample,
        "has_toc": has_toc,
    }


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
                f"ERROR: Unsupported format. Detected ZIP but not EPUB.\n"
                f"Supported: .pdf, .epub",
                file=sys.stderr,
            )
            sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if is_epub:
        print(f"Extracting EPUB: {input_path}")
        text, method = extract_epub(input_path)
        pages = count_epub_chapters(input_path)
        pages_label = "spine_items"
    else:
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

        pages = count_pages(input_path, extracted_text=text)
        pages_label = "pages"

    # Write full text
    OUTPUT_TEXT.write_text(text, encoding="utf-8")

    tokens = estimate_tokens(text)
    structure = detect_structure(text)
    file_size_mb = os.path.getsize(input_path) / (1024 * 1024)

    metadata = {
        "source_file": str(Path(input_path).resolve()),
        "filename": Path(input_path).name,
        "format": "epub" if is_epub else "pdf",
        "extraction_method": method,
        "extraction_mode_requested": requested_mode,
        "extraction_mode_used": extraction_mode,
        "file_size_mb": round(file_size_mb, 2),
        pages_label: pages,
        "chars": len(text),
        "words": len(text.split()),
        "estimated_tokens": tokens,
        "estimated_tokens_human": f"~{tokens // 1000}K",
        "output_text": str(OUTPUT_TEXT),
        **structure,
    }

    OUTPUT_META.write_text(json.dumps(metadata, indent=2, ensure_ascii=False))

    page_line = f"   {'Spine items' if is_epub else 'Pages'}: {pages}"
    print(f"\n📖 Extraction complete:")
    print(f"   Format  : {'EPUB' if is_epub else 'PDF'}")
    print(f"   Method  : {method}")
    print(page_line)
    print(f"   Words   : {len(text.split()):,}")
    print(f"   Tokens  : ~{tokens // 1000}K")
    print(f"   Chapters: {structure['chapters_detected']} detected")
    print(f"   ToC     : {'yes' if structure['has_toc'] else 'not detected'}")
    print(f"\n   Text → {OUTPUT_TEXT}")
    print(f"   Meta → {OUTPUT_META}")


if __name__ == "__main__":
    main()
