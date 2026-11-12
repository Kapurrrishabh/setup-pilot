"""Split documents into chunks that fit the embedding model's window: code at its definitions,
Markdown and reStructuredText at their headings. Each chunk keeps its file, section, lines and
kind."""

import ast
import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from setuppilot.chat.collect import Document, DocumentKind

ChunkKind = DocumentKind | Literal["card"]
CountTokens = Callable[[str], int]
# First line, last line (1-based, inclusive) and the section's name.
_Span = tuple[int, int, str]

_LINE_BREAK = re.compile(r"\r\n?|\n")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_RST_UNDERLINE = re.compile(r"^([=\-~^\"'`#*+])\1{2,}\s*$")
# The start of a definition in most languages: function, class, struct, impl, const and so on.
_DEFINITION = re.compile(
    r"^\s{0,4}(export\s+)?(default\s+)?(async\s+)?(pub(\([\w:]+\))?\s+)?(unsafe\s+)?"
    r"(function\*?|class|def|fn|struct|enum|trait|impl|mod|interface|type|const|let|var|func|"
    r"macro_rules!|static)\b"
)
_COMMENT = re.compile(r"^\s*(#|//|/\*|\*)")
# A class longer than this gets one chunk per method, so one method can be found on its own.
_BIG_CLASS_LINES = 60
_SECTION_CHARS = 120
# Lines a piece shares with the one before, so a fact cut at the edge is whole in one of them.
_OVERLAP_LINES = 2


@dataclass(frozen=True)
class Chunk:
    path: str
    section: str
    text: str
    # 1-based and inclusive; both 0 for text that is not lines of one file (the tree, the card).
    start_line: int
    end_line: int
    kind: ChunkKind

    @property
    def search_text(self) -> str:
        """What is embedded and word-searched: the text with its file and section in front."""
        return _header(self.path, self.section) + self.text


def chunk_document(document: Document, window: int, count_tokens: CountTokens) -> list[Chunk]:
    """Chunks whose search_text fits in `window` tokens, the embedding model's limit."""
    # Only real line breaks count, so line numbers match GitHub's (splitlines also breaks at \f).
    lines = _LINE_BREAK.split(document.text)
    costs = [count_tokens(line) + 1 for line in lines]  # + 1: the line break may be a token

    def budget(section: str) -> int:
        # A very long path leaves no room; then the model cuts the end off instead.
        return max(window - count_tokens(_header(document.path, section)), window // 2)

    if document.kind in ("code", "test"):
        spans = _merge_small(_code_spans(document.path, lines), costs, budget)
    elif document.path.lower().endswith((".md", ".rst")):
        spans = _prose_sections(document.path, lines)
    else:
        spans = [(1, len(lines), "")]
    chunks = []
    for first, last, section in spans:
        span_lines, span_costs = lines[first - 1 : last], costs[first - 1 : last]
        for start, end, text in _pack(span_lines, span_costs, first, budget(section), count_tokens):
            if document.kind == "tree":
                start = end = 0
            chunks.append(Chunk(document.path, section, text, start, end, document.kind))
    return chunks


def _header(path: str, section: str) -> str:
    return f"{path} {section}\n"


def _code_spans(path: str, lines: list[str]) -> list[_Span]:
    spans = _python_spans("\n".join(lines)) if path.endswith(".py") else None
    if spans is None:
        starts = [n for n, line in enumerate(lines, start=1) if _DEFINITION.match(line)]
        ends = [n - 1 for n in starts[1:]] + [len(lines)] if starts else []
        spans = [(a, b, lines[a - 1].strip()[:80]) for a, b in zip(starts, ends, strict=True)]
    return _fill_gaps(_with_comments_above(spans, lines), len(lines))


def _python_spans(text: str) -> list[_Span] | None:
    """Top-level functions and classes, from Python's own parser. None if it does not parse."""
    try:
        with warnings.catch_warnings():
            # Repo code may have old escapes like "\d"; that is not ours to warn about.
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    spans: list[_Span] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        start, end = _first_line(node), node.end_lineno or node.lineno
        is_class = isinstance(node, ast.ClassDef)
        methods = [m for m in node.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)]
        if is_class and methods and end - start > _BIG_CLASS_LINES:
            spans.append((start, _first_line(methods[0]) - 1, f"class {node.name}"))
            spans += [
                (_first_line(m), m.end_lineno or m.lineno, f"{node.name}.{m.name}") for m in methods
            ]
        else:
            kind = "class" if is_class else "def"
            spans.append((start, end, f"{kind} {node.name}"))
    return spans


def _first_line(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> int:
    return min([node.lineno] + [d.lineno for d in node.decorator_list])


def _with_comments_above(spans: list[_Span], lines: list[str]) -> list[_Span]:
    """Each span starts at the comment lines right above it, not the span before: such a
    comment often says why the code is written that way."""
    moved: list[_Span] = []
    for first, last, section in spans:
        floor = moved[-1][0] if moved else 0
        while first - 1 > floor and _COMMENT.match(lines[first - 2]):
            first -= 1
        if moved and moved[-1][1] >= first:
            moved[-1] = (moved[-1][0], first - 1, moved[-1][2])
        moved.append((first, last, section))
    return moved


def _fill_gaps(spans: list[_Span], line_count: int) -> list[_Span]:
    """The spans plus unnamed ones for the lines between them (imports, constants, main code),
    so every line of the file is in some span."""
    filled: list[_Span] = []
    covered = 0
    for first, last, section in spans:
        if first > covered + 1:
            filled.append((covered + 1, first - 1, ""))
        if last > covered:
            filled.append((max(first, covered + 1), last, section))
            covered = last
    if covered < line_count:
        filled.append((covered + 1, line_count, ""))
    return filled


def _merge_small(spans: list[_Span], costs: list[int], budget: Callable[[str], int]) -> list[_Span]:
    """Neighbouring small spans (one-line constants, tiny helpers) joined while they fit in one
    chunk under the joined section's name."""
    merged: list[_Span] = []
    for first, last, section in spans:
        if merged:
            start, _, names = merged[-1]
            joined = ", ".join(name for name in (names, section) if name)[:_SECTION_CHARS]
            if sum(costs[start - 1 : last]) <= budget(joined):
                merged[-1] = (start, last, joined)
                continue
        merged.append((first, last, section))
    return merged


def _prose_sections(path: str, lines: list[str]) -> list[_Span]:
    """Markdown and reStructuredText split at their headings, outside code blocks."""
    starts: list[tuple[int, str]] = [(1, "")]
    is_rst = path.lower().endswith(".rst")
    in_code = False
    for n, line in enumerate(lines, start=1):
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
        if in_code:
            continue
        heading = None if is_rst else _MD_HEADING.match(line)
        if heading:
            starts.append((n, heading.group(2)))
        elif is_rst and n > 1 and _RST_UNDERLINE.match(line):
            title = lines[n - 2].strip()
            if title and not _RST_UNDERLINE.match(lines[n - 2]):
                starts.append((n - 1, title))
    ends = [n - 1 for n, _ in starts[1:]] + [len(lines)]
    return [(a, b, title) for (a, title), b in zip(starts, ends, strict=True) if b >= a]


def _pack(
    lines: list[str], costs: list[int], first: int, budget: int, count_tokens: CountTokens
) -> list[tuple[int, int, str]]:
    """Pieces of whole lines of at most `budget` tokens, with their line numbers. A line longer
    than the budget is split at spaces."""
    pieces = []
    start = 0
    while start < len(lines):
        end, used = start, 0
        while end < len(lines) and used + costs[end] <= budget:
            used += costs[end]
            end += 1
        if end == start:
            for part in _split_line(lines[start], budget, count_tokens):
                pieces.append((first + start, first + start, part))
            start += 1
            continue
        if (piece := _trimmed(lines, start, end, first)) is not None:
            pieces.append(piece)
        fits_overlap = sum(costs[end - _OVERLAP_LINES : end + 1]) <= budget
        has_room = end - start > _OVERLAP_LINES
        start = end - _OVERLAP_LINES if end < len(lines) and fits_overlap and has_room else end
    return pieces


def _trimmed(lines: list[str], start: int, end: int, first: int) -> tuple[int, int, str] | None:
    """Lines start..end without blank lines at either edge, or None if all are blank."""
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    if start == end:
        return None
    return first + start, first + end - 1, "\n".join(lines[start:end])


def _split_line(line: str, budget: int, count_tokens: CountTokens) -> list[str]:
    """One over-long line in pieces at spaces. A single word over the budget is cut every
    `budget` characters, which is safe because a token is at least one character."""
    parts: list[str] = []
    words: list[str] = []
    used = 0
    for word in line.split():
        cost = count_tokens(word)
        if words and used + cost > budget:
            parts.append(" ".join(words))
            words, used = [], 0
        if cost > budget:
            parts += [word[n : n + budget] for n in range(0, len(word), budget)]
            continue
        words.append(word)
        used += cost
    if words:
        parts.append(" ".join(words))
    return parts
