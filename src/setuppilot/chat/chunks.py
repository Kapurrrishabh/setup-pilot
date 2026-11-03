"""Split documents into small, slightly overlapping chunks that remember their file and section."""

import re
from dataclasses import dataclass

from setuppilot.chat.collect import Document

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


@dataclass(frozen=True)
class Chunk:
    path: str
    section: str
    text: str


def chunk_documents(documents: list[Document], size: int, overlap: int) -> list[Chunk]:
    chunks = []
    for document in documents:
        for section, text in _sections(document):
            chunks += [
                Chunk(document.path, section, piece) for piece in _pieces(text, size, overlap)
            ]
    return chunks


def _sections(document: Document) -> list[tuple[str, str]]:
    """Markdown files are split at their headings; other files are one section."""
    if not document.path.lower().endswith(".md"):
        return [("", document.text)]
    sections, title, lines = [], "", []
    in_code = False
    for line in document.text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        heading = None if in_code else _HEADING.match(line)
        if heading:
            if "".join(lines).strip():
                sections.append((title, "\n".join(lines)))
            title, lines = heading.group(2), [line]
        else:
            lines.append(line)
    if "".join(lines).strip():
        sections.append((title, "\n".join(lines)))
    return sections


def _pieces(text: str, size: int, overlap: int) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    pieces, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        # Prefer to cut at a line break, so a piece does not end mid-sentence.
        cut = text.rfind("\n", start + size // 2, end) if end < len(text) else end
        end = cut if cut > start else end
        pieces.append(text[start:end].strip())
        if end >= len(text):
            break
        # Always move forward, even when a line break forced a short piece.
        start = max(end - overlap, start + 1)
    return [p for p in pieces if p]
