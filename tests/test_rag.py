"""Required test 4: RAG refuses when the PDF has no answer. Hallucination checks run in code, not in the prompt.

These tests need no embedding server: the question vector is replaced by a hand-made one.
"""
import math

import pytest

from app import agent
from app.config import settings
from app.tools import rag
from helpers import llm_text, llm_tool_call, query


def vector(*values: float) -> str:
    """A pgvector literal of the right size: the given values first, then zeros."""
    values = list(values) + [0.0] * (settings.embedding_dim - len(values))
    return "[" + ",".join(str(v) for v in values) + "]"


@pytest.fixture
def two_clauses():
    """Two fake clauses whose vectors point in different directions (cosine similarity 0 to each other)."""
    for clause_no, embedding in (("6.5.1", vector(1.0)), ("9.9.9", vector(0.0, 0.0, 1.0))):
        query("""INSERT INTO document_chunks (document_title, clause_no, page_from, page_to, content, embedding)
                 VALUES ('TEST', %s, 1, 1, 'test clause', %s::vector) RETURNING id""", [clause_no, embedding])
    yield
    query("DELETE FROM document_chunks WHERE document_title = 'TEST' RETURNING id")


def ask_with_similarity(monkeypatch, similarity: float) -> dict:
    """Search with a question vector whose cosine similarity to clause 6.5.1 is exactly `similarity`."""
    question = vector(similarity, math.sqrt(1 - similarity ** 2))
    monkeypatch.setattr(rag, "embed_query", lambda text: question)
    return rag.search_documents("any question")


def test_close_match_is_returned(monkeypatch, two_clauses):
    result = ask_with_similarity(monkeypatch, 0.9)
    assert result["found"] is True
    assert [source["clause"] for source in result["sources"]] == ["6.5.1"]


def test_weak_match_is_refused_in_code(monkeypatch, two_clauses):
    result = ask_with_similarity(monkeypatch, rag.MIN_SIMILARITY - 0.01)
    assert result["found"] is False
    assert "sources" not in result  # the LLM never sees the weak clause


def test_match_just_above_threshold_is_returned(monkeypatch, two_clauses):
    assert ask_with_similarity(monkeypatch, rag.MIN_SIMILARITY + 0.001)["found"] is True


def test_long_clause_is_returned_whole(monkeypatch):
    """A clause stored in two parts comes back as one source with all its text (6.5.5 keeps its exception list)."""
    for part_no, embedding, text in ((1, vector(1.0), "first part"), (2, vector(0.0, 0.0, 0.0, 1.0), "exception")):
        query("""INSERT INTO document_chunks (document_title, clause_no, part_no, page_from, page_to, content, embedding)
                 VALUES ('TEST', '7.7.7', %s, 1, 1, %s, %s::vector) RETURNING id""", [part_no, text, embedding])
    monkeypatch.setattr(rag, "embed_query", lambda text: vector(1.0))
    try:
        sources = rag.search_documents("any question")["sources"]
        assert [(s["clause"], s["text"]) for s in sources] == [("7.7.7", "first part\nexception")]
    finally:
        query("DELETE FROM document_chunks WHERE document_title = 'TEST' RETURNING id")


def test_empty_question_is_an_error():
    assert "error" in rag.search_documents("   ")


# ---------- citation check ----------

SOURCES = [{"clause": "6.5.1"}, {"clause": "6.5.2"}, {"clause": "2"}]


@pytest.mark.parametrize("answer", [
    "Within 10 days (User Agreement, clause 6.5.1).",
    "10 kun ichida (Foydalanuvchi kelishuvi, 6.5.1-band).",
    "Согласно п. 6.5.2 Пользовательского соглашения.",
    "See section 6.5, clause 6.5.1.",
    "Terms are defined in clause 2.",
    "Price 15,000,000 UZS, clause 6.5.1.",
    "Clause 6.5.1 and Resolution No. 75 of 13.02.2003.",
])
def test_citations_that_pass(answer):
    assert rag.check_citations(answer, SOURCES) == []


@pytest.mark.parametrize("answer, problem", [
    ("You can return it within 14 days.", "cites no clause"),
    ("Per clause 6.5.1 and clause 8.2.4.", "8.2.4"),
    ("3-band bo'yicha mumkin, 6.5.1.", "Clause 3"),
])
def test_citations_that_fail(answer, problem):
    problems = rag.check_citations(answer, SOURCES)
    assert any(problem in p for p in problems)


def test_refusal_needs_no_citation():
    assert rag.check_citations("The documents do not contain this.", []) == []


def test_clause_without_any_retrieved_source_fails():
    assert rag.check_citations("By clause 6.5.1 you can.", []) != []


# ---------- through the agent: an invented clause never reaches the client ----------

def fake_search(found: bool):
    def search(query: str) -> dict:
        if not found:
            return {"found": False, "note": "not in the documents"}
        return {"found": True, "sources": [{"document": "User Agreement", "clause": "6.5.1", "text": "10 days"}]}
    return search


def test_agent_replaces_an_answer_with_an_invented_clause(conversation, scripted_llm, monkeypatch):
    monkeypatch.setitem(agent.TOOLS["search_documents"], "function", fake_search(found=True))
    scripted_llm(
        llm_tool_call("search_documents", query="возврат"),
        llm_text("Within 14 days (User Agreement, clause 8.2.4)."),   # invented clause
        llm_text("Within 14 days (clause 8.2.4)."),                    # still invented after the retry
    )
    answer = agent.handle_message(conversation, "Can I return a laptop?")
    assert answer == agent.FALLBACK["en"]


def test_agent_accepts_a_correctly_cited_answer_after_retry(conversation, scripted_llm, monkeypatch):
    monkeypatch.setitem(agent.TOOLS["search_documents"], "function", fake_search(found=True))
    scripted_llm(
        llm_tool_call("search_documents", query="возврат"),
        llm_text("Within 10 days."),                                   # no citation: rejected
        llm_text("Within 10 days (User Agreement, clause 6.5.1)."),
    )
    assert "6.5.1" in agent.handle_message(conversation, "Can I return a laptop?")


def test_agent_refusal_when_nothing_is_found(conversation, scripted_llm, monkeypatch):
    monkeypatch.setitem(agent.TOOLS["search_documents"], "function", fake_search(found=False))
    scripted_llm(
        llm_tool_call("search_documents", query="ИНН компании"),
        llm_text("Our tax ID is 309376127 (clause 2.1)."),             # hallucination: rejected
        llm_text("The documents do not contain our tax ID."),
    )
    answer = agent.handle_message(conversation, "What is your company's tax ID?")
    assert answer == "The documents do not contain our tax ID."
    