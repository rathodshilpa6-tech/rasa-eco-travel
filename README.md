# EcoTrip Advisor — Sustainable Travel Chatbot (Rasa)

A Rasa Open Source conversational agent that helps travellers plan
lower-carbon trips: eco-certified accommodation, green transport, carbon
footprint estimates, and seamless handover to human travel advisors for
complex itineraries.

## Project layout

```
rasa-eco-travel/
├── actions/actions.py        # Custom actions: APIs, scoring, handover
├── data/nlu.yml               # NLU training examples
├── data/stories.yml           # Multi-turn dialogue stories
├── data/rules.yml             # Fallback + always-on rules
├── domain.yml                 # Intents, entities, slots, responses
├── config.yml                 # NLU pipeline + dialogue policies
├── mock_data/                 # Curated static datasets (no reliable free API)
├── frontend/streamlit_app.py  # Prototype UI wired to the Rasa REST channel
├── endpoints.yml / credentials.yml  # action server URL, REST channel
├── Makefile                   # isolated setup / train / run / test commands
├── tests/test_stories.yml     # `rasa test core` stories
├── tests/test_actions.py      # pytest unit tests for custom actions
├── Dockerfile / Dockerfile.actions / docker-compose.yml
├── .env.example
└── requirements.txt
```

## 1. Local setup (isolated)

Requires [uv](https://docs.astral.sh/uv/) and `make`. Everything stays inside
this folder: Python 3.10 goes in `.python/`, packages in `.venv/`, and `make`
redirects `HOME` to `.home/` because Rasa, TensorFlow and Streamlit otherwise
write config files into your home directory.

```bash
make setup                 # Python 3.10 + dependencies (Rasa 3.6 needs Python <= 3.10)
cp .env.example .env       # optional: API keys; without them the bot uses labelled fallback data
```

## 2. Train the model

```bash
make validate              # domain / stories / rules consistency
make train
```

## 3. Run locally (three terminals)

```bash
make actions               # action server on :5055
make rasa                  # NLU + Core server on :5005 (REST channel)
make ui                    # Streamlit UI on http://localhost:8501
```

`make shell` chats in the terminal instead of the UI (needs `make actions`).

## 4. Testing

```bash
make test                  # pytest unit tests for custom actions
make test-core             # dialogue tests (tests/test_stories.yml)
make test-nlu              # NLU cross-validation
```

## 5. Docker deployment

```bash
docker compose up --build
```

This starts three containers: `rasa` (5005), `action-server` (5055,
internal-only in production — do not expose publicly), and `frontend`
(3000 → Streamlit on 8501 internally).

## 6. Cloud deployment (HuggingFace Spaces — recommended, zero-cost)

1. Create a new Space, SDK = Docker.
2. Push this repository to the Space's git remote.
3. Add `CLIMATIQ_API_KEY`, `AMADEUS_CLIENT_ID`, `AMADEUS_CLIENT_SECRET`,
   `OPENCAGE_API_KEY` as **Space secrets** (never in code or `.env` in the repo).
4. The Space builds `Dockerfile` and exposes port 5005; point the frontend's
   `RASA_REST_URL` secret at the Space's public URL.

AWS/Azure/GCP: use the same two Docker images (`Dockerfile`,
`Dockerfile.actions`) behind a container service (ECS Fargate, Azure
Container Apps, or Cloud Run), with the action server kept on an internal
network and only the Rasa REST/webhook endpoint exposed. Use `pyngrok` only
for local development tunnelling, never for production traffic.

## Notes on data & ethics

- No verified free API exists for cross-destination eco-certification or
  community-based tourism listings, so `mock_data/*.json` is used as a
  transparent, clearly-labelled curated dataset — the bot always tells the
  user when a recommendation comes from this static list rather than a
  live, verified source, to avoid unsubstantiated ("greenwashed") claims.
- `.env` is git-ignored; only `.env.example` (empty placeholders) is committed.
- Conversation transcripts packaged for human handover are limited to the
  last 20 turns and are not persisted beyond the handover event, in line
  with data-minimisation principles under GDPR.
