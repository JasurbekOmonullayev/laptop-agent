"""RAG with the real PDF chunks and the real embedding model.

The chunks are copied from the real database (read only). Skipped when the embedding server is not running
or the PDF has not been ingested yet.
"""
import httpx
import psycopg
import pytest

from app.config import settings
from app.db import connect
from app.tools import rag

COLUMNS = "document_title, section_title, clause_no, part_no, page_from, page_to, content, embedding"


@pytest.fixture(scope="module", autouse=True)
def pdf_chunks(real_database_conninfo):
    try:
        httpx.get(settings.embedding_base_url, timeout=3)
    except httpx.HTTPError:
        pytest.skip("embedding server is not running")
    with psycopg.connect(real_database_conninfo) as real:
        rows = real.execute(f"SELECT {COLUMNS.replace('embedding', 'embedding::text')} FROM document_chunks").fetchall()
    if not rows:
        pytest.skip("the PDF has not been ingested into the real database")

    with connect() as conn, conn.cursor() as cur:
        cur.executemany(f"INSERT INTO document_chunks ({COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector)",
                        rows)
    yield
    with connect() as conn:
        conn.execute("DELETE FROM document_chunks")


@pytest.mark.parametrize("question", [
    "ИНН магазина",                      # sample question 18: the trap
    "юридический адрес магазина",
    "директор компании",
    "время работы батареи ноутбука",
    "How do I bake a chocolate cake?",
])
def test_questions_not_in_the_pdf_are_refused(question):
    assert rag.search_documents(question)["found"] is False


@pytest.mark.parametrize("question, clause", [
    ("возврат товара надлежащего качества срок", "6.5.1"),                       # sample question 13
    ("требования покупателя при обнаружении недостатков товара", "6.6.1"),       # sample question 15
    ("срок хранения заказа в пункте выдачи", "6.4.3"),                           # sample question 16
    ("возврат стоимости доставки при отказе от заказа с недостатками", "6.3.2"),  # sample question 17
])
def test_questions_in_the_pdf_find_the_right_clause(question, clause):
    result = rag.search_documents(question)
    assert result["found"] is True
    assert result["sources"][0]["clause"] == clause
    