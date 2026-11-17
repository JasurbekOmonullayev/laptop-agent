# Decisions

## 1. Database schema

`laptops.stock_qty` is never changed by a booking. Available units are computed by the view `laptop_availability`
as stock minus active bookings (`confirmed` and not yet expired), so stock and holds cannot drift apart, and an
expired hold frees the stock at once. Business rules also live in the schema as constraints: quantity 1-3 (P04),
phone `+998XXXXXXXXX`, `total = unit_price × quantity`, a status list, `cancelled_at` set only for cancelled
bookings. Every status change goes to `booking_status_log` with time and actor (client, agent or system).
Summaries waiting for a "yes" are stored in `pending_actions` (at most one open per conversation, valid 15 minutes),
and every message in `messages`, so the state survives a restart.

## 2. Data cleaning

`scripts/import_warehouse.py` keeps every row (511) and never guesses a value. "min 16" becomes 16 with
`is_min = true`, so the agent can say "at least 16 GB". A value outside a plausible range becomes NULL ("not
specified") instead of a wrong fact: RAM 4-256 GB, storage 64 GB-16 TB, screen 10-19", warranty and delivery
1-60. Resolutions are normalised to `WxH`; size classes like "≥1920" are not resolutions and become NULL. Prices
are rounded to whole soums. The script fails on a negative stock or a non-positive price. Cleaning report:

| Column | Change | Rows |
|---|---|---|
| ram_gb / storage_gb / screen_inch | "min N" → N, `is_min = true` | 83 / 60 / 140 |
| ram_gb / storage_gb | "min 2", 524288 GB → NULL | 6 / 6 |
| resolution | size class → NULL | 31 |
| warranty_months | 90, 100, 180, 365 → NULL | 19 |
| delivery_days | 95-365 → NULL | 7 |
| price_uzs | rounded to whole soum | 2 |

Values that are possible but suspicious (a ROG Strix with RTX 5050 at 3,850,000 UZS, a Teclast with 128 GB RAM)
are kept; the agent advises confirming such prices with the shop.

## 3. Chunking and retrieval

The PDF is split by its own clause numbers: one chunk is one clause (e.g. 6.5.1), with the document title,
section title and pages, because every answer must cite a clause. Pure headings are skipped; clauses over 1500
characters are split into parts; when a clause is found, all its parts are returned together, so an
exception at the end of a long clause is never cut off (6.5.5 lists household computing equipment among goods that
cannot be returned). A line like "309376127." is not taken as a clause, since clause numbers must
follow each other. The embedded text is the document and section title plus the clause, so short clauses still
match. Embeddings: `bge-m3` (multilingual, 1024 dimensions), local in Ollama. Search: cosine similarity in
pgvector, top 5, and only clauses with similarity ≥ 0.62. The threshold was measured: real questions in Russian
scored 0.65-0.78, questions the PDF does not answer (our tax ID, address, director) 0.45-0.60. The PDF is in
Russian, so the LLM rewrites the question in Russian before searching.

## 4. How the agent picks a tool

No framework and no separate router. The LLM sees nine tools with a description each, and the system prompt maps
question types to them: laptops → product tools, how the shop works → `get_shop_policies`, trade terms →
`search_documents`, bookings → `prepare_*` / `get_booking`. Code limits what the choice can do: the conversation
ID is added by code, unknown tools and bad arguments return an error, at most 6 tool rounds per answer. Only
user and assistant text is kept as history, so facts are fetched again in every turn.

## 5. How the client's confirmation is enforced

The LLM can only prepare. `prepare_booking`, `prepare_change` and `prepare_cancellation` check everything and save
the action and its summary in `pending_actions`; none writes a booking. There is no tool that confirms. When the
next client message arrives, `agent.py` checks it in code, before the LLM sees it: only a whole-message "yes"
from a fixed list ("ha", "yes", "да", ...) runs `confirm_pending`. That function uses the saved action, not new
LLM arguments, so exactly what the client saw is booked. It locks the laptop row (`SELECT ... FOR UPDATE`),
checks stock, limit and price again, and writes the booking and its log in one transaction. Without the lock,
30 parallel clients booked 29 units of a laptop that had 5; with it, exactly 5 (`tests/test_stock.py`).
The LLM can only prepare a booking for an offer number that already appeared in the conversation (typed by the
client or returned by a tool); a guessed number is refused in code.

## 6. How hallucination is prevented

Facts come only from tools: prices are formatted by code, stock is the computed available amount, and the prompt
forbids memory. For the PDF, weak matches are dropped in code, so the LLM never sees them. After the answer, code
checks it: every cited clause number must be among the clauses retrieved in this turn, an answer from the PDF must
cite at least one clause, and every booking ID must come from a tool result (a client typing a fake
"[System note]" cannot make the agent announce a booking). A failing answer is rewritten once with the exact
problem; if it still fails, a fixed safe reply is sent. All of this is tested with a scripted LLM
(`tests/test_rag.py`, `tests/test_confirmation.py`).

## 7. LLM

`qwen/qwen3.8-27b` through OpenRouter: open weights, 27B parameters. We compared nine open models under 200B on
the sample questions with the same tools (Qwen3.8-27B, Qwen3.8-Flash, Qwen3.6-35B-A3B, Gemma 4 31B,
Muse Glimmer 30B, Nemotron 3.5 Super VL, Ling 3.0 Flash, Ripple 65B preview, Step 3.7 Flash). Others suggested
splitting a 5-unit booking, mixed English into Uzbek answers or broke the price format; Qwen3.8-27B gave 27/27
clean answers. Step 3.7 Flash was excluded because its Hugging Face page lists 201B parameters.
