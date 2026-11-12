"""Markdown-aware chunking: split each knowledge-base document by heading, then by size."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_TABLE_RULE = re.compile(r"^\|?\s*:?-{3,}")
_EMPHASIS = re.compile(r"(\*\*|__|`)")


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    body: str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    section: str
    text: str

    @property
    def index_text(self) -> str:
        """What the rankers see: the title and section heading carry a lot of the signal."""
        return f"{self.title}. {self.section}. {self.text}"


def load_documents(kb_dir: str | Path) -> list[Document]:
    paths = sorted(Path(kb_dir).glob("*.md"))
    if not paths:
        raise FileNotFoundError(f"no knowledge-base documents (*.md) in {kb_dir}")
    documents = [parse_document(p.read_text(encoding="utf-8"), fallback_id=p.stem) for p in paths]
    ids = [d.doc_id for d in documents]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate doc_id in knowledge base")
    return documents


def parse_document(text: str, *, fallback_id: str) -> Document:
    """Read YAML front matter (`doc_id`, `title`); fall back to the file name and first heading."""
    meta: dict[str, object] = {}
    match = _FRONT_MATTER.match(text)
    if match:
        meta = yaml.safe_load(match.group(1)) or {}
        text = text[match.end() :]
    title = str(meta.get("title") or _first_heading(text) or fallback_id)
    return Document(doc_id=str(meta.get("doc_id") or fallback_id), title=title, body=text)


def chunk_document(
    doc: Document, *, max_words: int = 150, min_words: int = 60, overlap_words: int = 25
) -> list[Chunk]:
    """Chunk by section. Long sections are split into overlapping windows of whole blocks;
    short neighbouring sections are merged so each chunk carries enough context to rank well.
    """
    pieces: list[tuple[str, list[str]]] = []
    for section, blocks in _sections(doc):
        for window in _windows(blocks, max_words, overlap_words):
            previous = pieces[-1][1] if pieces else None
            if previous and _words(previous) < min_words and _words(previous + window) <= max_words:
                pieces[-1] = (f"{pieces[-1][0]} / {section}", previous + window)
            else:
                pieces.append((section, window))
    return [
        Chunk(
            chunk_id=f"{doc.doc_id}#{n}",
            doc_id=doc.doc_id,
            title=doc.title,
            section=section,
            text="\n".join(blocks),
        )
        for n, (section, blocks) in enumerate(pieces)
    ]


def chunk_documents(documents: list[Document], **options: int) -> list[Chunk]:
    return [chunk for doc in documents for chunk in chunk_document(doc, **options)]


def _first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if heading := _HEADING.match(line):
            return heading.group(2)
    return None


def _sections(doc: Document) -> list[tuple[str, list[str]]]:
    """(section heading, blocks) pairs. A block is a paragraph, list item or table row."""
    sections: list[tuple[str, list[str]]] = []
    trail: list[str] = []  # headings below the title, e.g. ["Fasting", "Diabetes"]
    blocks: list[str] = []
    paragraph: list[str] = []
    in_list_item = False  # wrapped continuation lines belong to the previous list item

    def end_paragraph() -> None:
        if paragraph:
            blocks.append(" ".join(paragraph))
            paragraph.clear()

    def end_section() -> None:
        end_paragraph()
        if blocks:
            sections.append((" > ".join(trail) or doc.title, blocks.copy()))
            blocks.clear()

    for raw in doc.body.splitlines():
        line = _EMPHASIS.sub("", raw).strip()
        is_list_item = line.startswith(("- ", "* ", "+ ")) or bool(re.match(r"^\d+\.\s", line))
        if heading := _HEADING.match(line):
            end_section()
            level, name = len(heading.group(1)), heading.group(2)
            if level > 1:  # the H1 repeats the title
                del trail[level - 2 :]
                trail.append(name)
        elif not line:
            end_paragraph()
        elif is_list_item:
            end_paragraph()
            blocks.append(line)
        elif line.startswith("|"):
            end_paragraph()
            if not _TABLE_RULE.match(line):
                cells = [c.strip() for c in line.strip("|").split("|")]
                blocks.append(": ".join(c for c in cells if c))
        elif in_list_item:
            blocks[-1] += " " + line
            continue
        else:
            paragraph.append(line)
        in_list_item = is_list_item
    end_section()
    return sections


def _windows(blocks: list[str], max_words: int, overlap_words: int) -> list[list[str]]:
    """Greedily pack blocks into windows of <= max_words, repeating trailing blocks as overlap."""
    units = [piece for block in blocks for piece in _split_long(block, max_words)]
    windows: list[list[str]] = []
    current: list[str] = []
    fresh = 0  # blocks in `current` that are not overlap carried from the previous window
    for unit in units:
        if fresh and _words(current) + _words([unit]) > max_words:
            windows.append(current)
            current = _tail(current, min(overlap_words, max_words - _words([unit])))
            fresh = 0
        current.append(unit)
        fresh += 1
    if fresh:
        windows.append(current)
    return windows


def _tail(blocks: list[str], budget: int) -> list[str]:
    tail: list[str] = []
    for block in reversed(blocks):
        if _words(tail) + _words([block]) > budget:
            break
        tail.insert(0, block)
    return tail


def _split_long(block: str, max_words: int) -> list[str]:
    """Split an oversized block at sentence ends (or, for a run-on sentence, between words)."""
    if len(block.split()) <= max_words:
        return [block]
    pieces: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", block):
        tokens = sentence.split()
        for start in range(0, len(tokens), max_words):
            piece = " ".join(tokens[start : start + max_words])
            if pieces and len(pieces[-1].split()) + len(piece.split()) <= max_words:
                pieces[-1] += " " + piece
            else:
                pieces.append(piece)
    return pieces


def _words(blocks: list[str]) -> int:
    return sum(len(b.split()) for b in blocks)
