"""Convert course files and Canvas HTML into markdown text."""
from __future__ import annotations

import json
import re
from pathlib import Path

NO_TEXT = "(no extractable text on this page; open the raw file to see it)"
MAX_TEXT_CHARS = 400_000

CODE_EXTS = {
    ".r", ".rmd", ".qmd", ".py", ".java", ".c", ".h", ".cpp", ".hpp", ".js", ".ts", ".sql",
    ".m", ".jl", ".hs", ".sh", ".tex",
}
PLAIN_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml", ".xml"}
DOC_EXTS = {".pdf", ".pptx", ".docx", ".ipynb", ".html", ".htm", ".vtt", ".srt"}
DATA_EXTS = {".csv", ".tsv", ".json", ".xml"}
DATA_PREVIEW_CHARS = 4000


def supported(ext: str) -> bool:
    return ext.lower() in CODE_EXTS | PLAIN_EXTS | DOC_EXTS


def to_markdown(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdf(path)
    if ext == ".pptx":
        return _pptx(path)
    if ext == ".docx":
        return _docx(path)
    if ext == ".ipynb":
        return _notebook(path)
    if ext in (".html", ".htm"):
        return html_to_markdown(path.read_text(encoding="utf-8", errors="replace"))
    if ext in (".vtt", ".srt"):
        return captions_to_markdown(path.read_text(encoding="utf-8", errors="replace"))
    text = path.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_CHARS]
    if ext in DATA_EXTS and len(text) > DATA_PREVIEW_CHARS:
        # Datasets drown out real content in search; keep a preview only.
        cut = text.rfind("\n", 0, DATA_PREVIEW_CHARS)
        return text[:cut if cut > 0 else DATA_PREVIEW_CHARS] + f"\n\n(data preview only; full file is {len(text):,} chars)"
    if ext in CODE_EXTS and ext not in (".rmd", ".qmd", ".tex"):
        return f"```{ext.lstrip('.')}\n{text}\n```"
    return text


def _pdf(path: Path) -> str:
    import pymupdf

    parts = []
    with pymupdf.open(path) as doc:
        for number, page in enumerate(doc, 1):
            text = page.get_text("text").strip()
            parts.append(f"## Page {number}\n\n{text or NO_TEXT}")
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


def _pptx(path: Path) -> str:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    def walk(shapes):
        for shape in shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from walk(shape.shapes)
            else:
                yield shape

    parts = []
    for number, slide in enumerate(Presentation(str(path)).slides, 1):
        lines = []
        for shape in walk(slide.shapes):
            if shape.has_text_frame and shape.text_frame.text.strip():
                lines.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    lines.append(" | ".join(cell.text.strip() for cell in row.cells))
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                lines.append(f"Speaker notes: {notes}")
        parts.append(f"## Slide {number}\n\n" + ("\n".join(lines) or NO_TEXT))
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


def _docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    lines = []
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = (para.style.name or "").lower() if para.style is not None else ""
        level = re.search(r"heading (\d)", style)
        lines.append(f"{'#' * (int(level.group(1)) + 1)} {text}" if level else text)
    for table in document.tables:
        for row in table.rows:
            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
    return "\n\n".join(lines)[:MAX_TEXT_CHARS]


def _notebook(path: Path) -> str:
    notebook = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    language = (notebook.get("metadata", {}).get("kernelspec", {}) or {}).get("language", "")
    parts = []
    for cell in notebook.get("cells", []):
        source = "".join(cell.get("source", []))
        if not source.strip():
            continue
        parts.append(source if cell.get("cell_type") == "markdown" else f"```{language}\n{source}\n```")
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


def html_to_markdown(html: str) -> str:
    """Canvas HTML to markdown. Canvas equation images become inline LaTeX."""
    if not html.strip():
        return ""
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for img in soup.find_all("img"):
        latex = img.get("data-equation-content")
        if latex:
            img.replace_with(f" ${latex}$ ")
    text = markdownify(
        str(soup), heading_style="ATX", strip=["img"], escape_underscores=False, escape_asterisks=False
    )
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:MAX_TEXT_CHARS]


_CUE_TIME = re.compile(r"(\d{1,2}:)?(\d{2}):(\d{2})[.,]\d{3}\s*-->")


def captions_to_markdown(text: str, paragraph_seconds: int = 60) -> str:
    """VTT/SRT captions to timestamped paragraphs of roughly one minute each."""
    paragraphs: list[str] = []
    current: list[str] = []
    start = None
    last_line = ""
    for raw in text.splitlines():
        line = raw.strip()
        time = _CUE_TIME.search(line)
        if time:
            seconds = int((time.group(1) or "0:")[:-1]) * 3600 + int(time.group(2)) * 60 + int(time.group(3))
            if start is None:
                start = seconds
            elif seconds - start >= paragraph_seconds and current:
                paragraphs.append(_stamp(start) + " ".join(current))
                current, start = [], seconds
            continue
        if not line or line == "WEBVTT" or line.isdigit() or line.startswith(("NOTE", "Kind:", "Language:")):
            continue
        line = re.sub(r"<[^>]+>", "", line)
        if line != last_line:
            current.append(line)
            last_line = line
    if current:
        paragraphs.append(_stamp(start or 0) + " ".join(current))
    return "\n\n".join(paragraphs)[:MAX_TEXT_CHARS]


def _stamp(seconds: int) -> str:
    return f"[{seconds // 3600:d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}] "
