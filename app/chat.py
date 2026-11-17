"""Terminal chat with the agent.

Run:  python -m app.chat                     a new conversation
      python -m app.chat <conversation_id>   continue an earlier one
"""
import logging
import sys
import uuid

from app.agent import handle_message, new_conversation
from app.db import connect

EXIT_WORDS = {"exit", "quit", "chiqish", "выход"}


def conversation_exists(conversation_id: str) -> bool:
    try:
        uuid.UUID(conversation_id)
    except ValueError:
        return False
    with connect() as conn:
        row = conn.execute("SELECT 1 FROM conversations WHERE conversation_id = %s", [conversation_id]).fetchone()
    return row is not None


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if len(sys.argv) > 1:
        conversation_id = sys.argv[1]
        if not conversation_exists(conversation_id):
            sys.exit(f"No conversation {conversation_id}.")
    else:
        conversation_id = new_conversation()
    print(f"Conversation {conversation_id}. Type 'exit' to leave.\n")

    try:
        while True:
            text = input("You: ").strip()
            if not text:
                continue
            if text.lower() in EXIT_WORDS:
                break
            print(f"\nAgent: {handle_message(conversation_id, text)}\n")
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()
    