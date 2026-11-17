"""Web chat with the agent, built with Streamlit.

Run:  python -m streamlit run app/web.py
The conversation ID is kept in the page address (?c=...), so reloading the page continues the same conversation.
"""
import streamlit as st

from app.agent import handle_message, load_history, new_conversation
from app.chat import conversation_exists

st.set_page_config(page_title="Laptop shop assistant", page_icon="💻")


def start_new_conversation() -> None:
    st.session_state.conversation_id = new_conversation()
    st.session_state.messages = []
    st.query_params["c"] = st.session_state.conversation_id


# Streamlit runs this whole file again after every click; session_state keeps values between runs.
if "conversation_id" not in st.session_state:
    from_address = st.query_params.get("c")
    if from_address and conversation_exists(from_address):
        st.session_state.conversation_id = from_address
        st.session_state.messages = load_history(from_address)
    else:
        start_new_conversation()

with st.sidebar:
    st.title("💻 Laptop shop")
    st.caption("Ask in Uzbek, Russian or English.")
    if st.button("New conversation"):
        start_new_conversation()
        st.rerun()
    st.caption(f"Conversation: {st.session_state.conversation_id}")

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

if text := st.chat_input("Write a message..."):
    st.session_state.messages.append({"role": "user", "content": text})
    with st.chat_message("user"):
        st.markdown(text)
    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            answer = handle_message(st.session_state.conversation_id, text)
        st.markdown(answer)
    st.session_state.messages.append({"role": "assistant", "content": answer})
    