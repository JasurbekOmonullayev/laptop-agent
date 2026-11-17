"""Split data/uzum_trade_agreements.pdf into clause chunks, embed them, store in document_chunks.

One chunk = one numbered clause (e.g. 6.5.1) with its text. Every chunk keeps the
document title and clause number, so every answer can cite its source.
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psycopg
import pymupdf
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
PDF_PATH = PROJECT_DIR / "data" / "uzum_trade_agreements.pdf"

MAX_CHUNK_CHARS = 1500     # longer clauses are split into parts
HEADING_MAX_CHARS = 150    # a clause this short, followed by its own sub-clauses, is a heading
EMBED_BATCH_SIZE = 16

DOC_START = re.compile(r"^Document (\d+) of \d+$")
SOURCE_LINE = re.compile(r"^Source: (\S+)")
TOC_ITEM = re.compile(r"^(\d+)\.\s+(.+)$")
CLAUSE_START = re.compile(r"^(\d+(?:\.\d+)*)\.\s+(.*)$")


@dataclass
class Clause:
    number: str
    page_from: int
    page_to: int
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


def load_settings() -> dict:
    load_dotenv(PROJECT_DIR / ".env")
    return {
        "conninfo": psycopg.conninfo.make_conninfo(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=os.getenv("POSTGRES_PORT", "5432"),
            dbname=os.environ["POSTGRES_DB"],
            user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"],
        ),
        "embed_url": os.environ["EMBEDDING_BASE_URL"].rstrip("/") + "/api/embed",
        "embed_model": os.environ["EMBEDDING_MODEL"],
        "embed_dim": int(os.environ["EMBEDDING_DIM"]),
    }


def read_pdf_lines(path: Path) -> list[tuple[int, str]]:
    """Return every non-empty line of the PDF together with its page number."""
    lines = []
    with pymupdf.open(path) as pdf:
        for page_no, page in enumerate(pdf, start=1):
            for line in page.get_text().splitlines():
                if line.strip():
                    lines.append((page_no, line.strip()))
    return lines


def read_titles(lines: list[tuple[int, str]]) -> dict[str, str]:
    """Map each document's source URL to its title, using the contents list on page 1."""
    titles, current = {}, []
    for page_no, line in lines:
        if page_no != 1:
            break
        if line.startswith("https://"):
            if current:
                titles[line] = " ".join(current)
            current = []
        elif TOC_ITEM.match(line):
            current = [TOC_ITEM.match(line).group(2)]
        elif current:
            current.append(line)
    return titles


def is_next_number(previous: tuple[int, ...] | None, current: tuple[int, ...]) -> bool:
    """True if `current` can follow `previous` (6.4 -> 6.4.1, 6.5 or 7). Rejects lines like '309376127.'."""
    if previous is None:
        return current == (1,) or current == (1, 1)
    if current == previous + (1,):
        return True
    depth = len(current)
    return depth <= len(previous) and current[:-1] == previous[: depth - 1] and current[-1] == previous[depth - 1] + 1


def split_into_documents(lines: list[tuple[int, str]]) -> list[tuple[str, list[tuple[int, str]]]]:
    titles = read_titles(lines)
    documents, title, body = [], None, []
    for page_no, line in lines:
        if DOC_START.match(line):
            if title:
                documents.append((title, body))
            title, body = None, []
        elif title is None and SOURCE_LINE.match(line):
            title = titles[SOURCE_LINE.match(line).group(1)]
        elif title is not None:
            body.append((page_no, line))
    if title:
        documents.append((title, body))
    return documents


def split_into_clauses(body: list[tuple[int, str]]) -> list[Clause]:
    """Text before the first numbered line becomes the 'preamble' clause."""
    clauses = [Clause("preamble", body[0][0], body[0][0])]
    previous = None
    for page_no, line in body:
        match = CLAUSE_START.match(line)
        if match:
            number = tuple(int(part) for part in match.group(1).split("."))
            if is_next_number(previous, number):
                clauses.append(Clause(match.group(1), page_no, page_no, [line]))
                previous = number
                continue
        clauses[-1].lines.append(line)
        clauses[-1].page_to = page_no
    return [clause for clause in clauses if clause.text]


def split_text(text: str, limit: int) -> list[str]:
    """Split on line breaks into parts of at most `limit` characters."""
    parts, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    parts.append(current)
    return parts


def build_chunks(documents) -> list[dict]:
    chunks = []
    for title, body in documents:
        clauses = split_into_clauses(body)
        headings = {}
        for index, clause in enumerate(clauses):
            next_clause = clauses[index + 1] if index + 1 < len(clauses) else None
            has_children = next_clause is not None and next_clause.number.startswith(clause.number + ".")
            heading_text = CLAUSE_START.match(clause.lines[0]).group(2) if clause.number != "preamble" else ""
            headings[clause.number] = heading_text

            if has_children and len(clause.text) <= HEADING_MAX_CHARS:
                continue  # a pure heading like "6.5. Возврат Товара надлежащего качества"

            parent = clause.number.rsplit(".", 1)[0] if "." in clause.number else None
            section_title = headings.get(parent) or None
            for part_no, content in enumerate(split_text(clause.text, MAX_CHUNK_CHARS), start=1):
                chunks.append({
                    "document_title": title,
                    "section_title": section_title,
                    "clause_no": clause.number,
                    "part_no": part_no,
                    "page_from": clause.page_from,
                    "page_to": clause.page_to,
                    "content": content,
                })
    return chunks


def embedding_input(chunk: dict) -> str:
    """Text that is embedded: the clause plus its context, so short clauses still match."""
    header = f"{chunk['document_title']}. {chunk['section_title'] or ''}".strip()
    return f"{header}\n{chunk['content']}"


def embed(texts: list[str], settings: dict) -> list[list[float]]:
    vectors = []
    with httpx.Client(timeout=300) as client:
        for start in range(0, len(texts), EMBED_BATCH_SIZE):
            batch = texts[start:start + EMBED_BATCH_SIZE]
            response = client.post(settings["embed_url"], json={"model": settings["embed_model"], "input": batch})
            response.raise_for_status()
            for vector in response.json()["embeddings"]:
                if len(vector) != settings["embed_dim"]:
                    raise ValueError(f"model returned {len(vector)} numbers, EMBEDDING_DIM is {settings['embed_dim']}")
                vectors.append(vector)
    return vectors


INSERT_CHUNK = """
INSERT INTO document_chunks
    (document_title, section_title, clause_no, part_no, page_from, page_to, content, embedding)
VALUES
    (%(document_title)s, %(section_title)s, %(clause_no)s, %(part_no)s,
     %(page_from)s, %(page_to)s, %(content)s, %(embedding)s::vector)
"""


def main() -> None:
    settings = load_settings()
    documents = split_into_documents(read_pdf_lines(PDF_PATH))
    chunks = build_chunks(documents)

    vectors = embed([embedding_input(chunk) for chunk in chunks], settings)
    for chunk, vector in zip(chunks, vectors):
        chunk["embedding"] = "[" + ",".join(str(x) for x in vector) + "]"

    with psycopg.connect(settings["conninfo"]) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM document_chunks")
            cur.executemany(INSERT_CHUNK, chunks)

    print(f"documents: {len(documents)}")
    for title, _ in documents:
        count = sum(1 for chunk in chunks if chunk["document_title"] == title)
        print(f"  {count:4d} chunks  {title}")
    print(f"total:     {len(chunks)} chunks")


if __name__ == "__main__":
    main()