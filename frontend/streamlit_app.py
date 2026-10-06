"""
Lightweight prototype frontend for the EcoTrip Advisor chatbot.

Wired to a live Rasa REST channel (default: http://localhost:5005/webhooks/rest/webhook).
Implements the concrete UI elements required by the assignment:
  - dynamic quick-reply buttons (rendered from Rasa button payloads)
  - colour-coded carbon result cards (green/amber/red) from the bot's custom payload
  - a visible human-handover indicator (driven by the bot's custom payload)

Run with: make ui   (or: streamlit run frontend/streamlit_app.py)
"""

import os
import uuid

import requests
import streamlit as st

RASA_REST_URL = os.environ.get("RASA_REST_URL", "http://localhost:5005/webhooks/rest/webhook")
MODE_LABELS = {"flight": "✈️ Flight", "train": "🚆 Train", "coach": "🚌 Coach"}

st.set_page_config(page_title="EcoTrip Advisor", page_icon="🌍")
st.title("🌍 EcoTrip Advisor")
st.caption("Plan a lower-carbon trip — prototype UI over a live Rasa backend")

state = st.session_state
state.setdefault("history", [])          # list of (role, content)
state.setdefault("handed_over", False)
state.setdefault("sender_id", f"web-{uuid.uuid4().hex[:12]}")  # one conversation per browser session


def carbon_level(kg_co2e: float) -> tuple:
    """Colour-codes a per-passenger carbon figure: green (low) / amber / red (high)."""
    if kg_co2e < 75:
        return "LOW", "green"
    if kg_co2e < 150:
        return "MODERATE", "orange"
    return "HIGH", "red"


def send_message(text: str, shown_as: str = None) -> None:
    state.history.append(("user", shown_as or text))
    try:
        resp = requests.post(RASA_REST_URL, json={"sender": state.sender_id, "message": text}, timeout=10)
        resp.raise_for_status()
        bot_messages = resp.json()
    except requests.exceptions.RequestException:
        state.history.append(("bot", "⚠️ Could not reach the assistant backend. "
                                     "Are `make rasa` and `make actions` running?"))
        return

    for msg in bot_messages:
        if msg.get("text"):
            state.history.append(("bot", msg["text"]))
        if msg.get("buttons"):
            state.history.append(("buttons", msg["buttons"]))
        custom = msg.get("custom") or {}
        if custom.get("handover"):
            state.handed_over = True
        if custom.get("type") == "carbon_estimates":
            state.history.append(("carbon", custom))


def on_button(payload: str, title: str) -> None:
    send_message(payload, shown_as=title)


def render_carbon_card(card: dict) -> None:
    with st.container(border=True):
        st.markdown(f"**Emissions per passenger, one way (~{card['distance_km']:,} km assumed)**")
        for e in card["estimates"]:
            level, colour = carbon_level(e["kg_co2e"])
            st.markdown(
                f"{MODE_LABELS.get(e['mode'], e['mode'])}: **{e['kg_co2e']:.0f} kg CO2e** "
                f":{colour}[● {level}] — _{e['source']}_"
            )


# Handle new input before drawing the history so the reply shows on this run.
# (st.chat_input is always pinned to the bottom of the page.)
user_input = st.chat_input("Tell me about the trip you're planning…")
if user_input:
    send_message(user_input)

last_buttons_index = max((i for i, (role, _) in enumerate(state.history) if role == "buttons"), default=-1)

for i, (role, content) in enumerate(state.history):
    if role == "user":
        st.chat_message("user").write(content)
    elif role == "bot":
        st.chat_message("assistant").write(content)
    elif role == "carbon":
        with st.chat_message("assistant"):
            render_carbon_card(content)
    elif role == "buttons":
        # Only the latest set of quick replies is clickable; older ones are history.
        cols = st.columns(len(content))
        for j, (col, btn) in enumerate(zip(cols, content)):
            col.button(btn["title"], key=f"btn-{i}-{j}", on_click=on_button,
                       args=(btn["payload"], btn["title"]),
                       disabled=i != last_buttons_index or state.handed_over,
                       help=f"Send: {btn['title']}")

if state.handed_over:
    st.warning("🔄 **Human advisor handover in progress** — a specialist has your full trip "
               "context and will join shortly.")
