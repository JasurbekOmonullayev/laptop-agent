"""RAG over the trade agreements PDF: search with a similarity threshold, and a citation check in code."""
import re

import httpx

from app.config import settings
from app.db import connect

MIN_SIMILARITY = 0.62  # measured on Russian queries: real answers 0.65-0.78, out-of-PDF questions 0.45-0.60
TOP_K = 5

SEARCH_QUERY = """
SELECT document_title, section_title, clause_no, page_from, page_to, content,
       1 - (embedding <=> %(vector)s::vector) AS similarity
FROM document_chunks
ORDER BY embedding <=> %(vector)s::vector
LIMIT %(top_k)s
"""

FULL_CLAUSE = """
SELECT string_agg(content, E'\\n' ORDER BY part_no) AS text
FROM document_chunks
WHERE document_title = %s AND clause_no = %s
"""

DATE = re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{4}\b")                              # 13.02.2003 is a date, not a clause
DOTTED_REF = re.compile(r"\b\d+(?:\.\d+)+\b")                                   # 6.5.1
WORD_REF = re.compile(r"(?:clause|пункт\w*|п\.|band\w*)\s*(\d+)\b(?!\.\d)|(?<![\d.])(\d+)-band", re.IGNORECASE)  # clause 2, 2-band


def embed_query(text: str) -> str:
    """The question as a pgvector literal, made by the same model that embedded the PDF."""
    response = httpx.post(
        f"{settings.embedding_base_url}/api/embed",
        json={"model": settings.embedding_model, "input": [text]},
        timeout=60,
    )
    response.raise_for_status()
    vector = response.json()["embeddings"][0]
    if len(vector) != settings.embedding_dim:
        raise ValueError(f"embedding has {len(vector)} numbers, EMBEDDING_DIM is {settings.embedding_dim}")
    return "[" + ",".join(str(x) for x in vector) + "]"


def to_source(row: dict) -> dict:
    return {
        "document": row["document_title"],
        "section": row["section_title"],
        "clause": row["clause_no"],
        "pages": f"{row['page_from']}-{row['page_to']}",
        "similarity": round(row["similarity"], 3),
        "text": row["content"],
    }


def search_documents(query: str) -> dict:
    query = query.strip()
    if not query:
        return {"error": "The query is empty."}
    vector = embed_query(query)
    sources, seen = [], set()
    with connect() as conn:
        rows = conn.execute(SEARCH_QUERY, {"vector": vector, "top_k": TOP_K}).fetchall()
        for row in rows:
            clause = (row["document_title"], row["clause_no"])
            # The threshold is the hallucination guard: weak matches are dropped in code, not by the LLM.
            if row["similarity"] < MIN_SIMILARITY or clause in seen:
                continue
            seen.add(clause)
            # A long clause is stored in parts. The LLM gets the whole clause, so an exception in a later part
            # (e.g. 6.5.5: goods that cannot be returned) is never cut off.
            row["content"] = conn.execute(FULL_CLAUSE, clause).fetchone()["text"]
            sources.append(to_source(row))
    if not sources:
        return {
            "found": False,
            "note": "The trade agreements do not contain this. Tell the client so plainly. "
                    "Do not answer from general knowledge.",
        }
    return {
        "found": True,
        "note": "Answer only from these clauses. Cite the document and clause number for every statement.",
        "sources": sources,
    }


def cited_clauses(answer: str) -> set[str]:
    answer = DATE.sub(" ", answer)
    cited = set(DOTTED_REF.findall(answer))
    for after_word, before_band in WORD_REF.findall(answer):
        cited.add(after_word or before_band)
    return cited


def check_citations(answer: str, sources: list[dict]) -> list[str]:
    """Problems with the answer's citations; an empty list means the answer passes."""
    allowed = {source["clause"] for source in sources}
    cited = cited_clauses(answer)
    if sources and not cited:
        return ["The answer cites no clause."]
    problems = []
    for clause in sorted(cited):
        # "6.5" is accepted when 6.5.1 was retrieved: it is the section the clause belongs to.
        if clause not in allowed and not any(a.startswith(clause + ".") for a in allowed):
            problems.append(f"Clause {clause} was not retrieved in this turn.")
    return problems


TOOLS = {
    "search_documents": {
        "function": search_documents,
        "schema": {
            "name": "search_documents",
            "description": (
                "Search the shop's trade agreements (Uzum Market documents, in Russian): ordering, payment, delivery, "
                "pickup, returns, exchange, defects, warranty claims, personal data, promotions. "
                "Call it for every such question. The documents do not contain our shop's own company details "
                "(tax ID, address). Answer only from the returned clauses and cite document and clause number."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The question rewritten in Russian, the documents' language, "
                                       "e.g. 'срок хранения заказа в пункте выдачи'",
                    },
                },
                "required": ["query"],
            },
        },
    },
}
