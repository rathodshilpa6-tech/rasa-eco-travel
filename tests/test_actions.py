"""
Unit tests for custom actions using mocked API responses.
Run with: make test   (or: pytest tests/test_actions.py -v)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions import actions  # noqa: E402
from actions.actions import (  # noqa: E402
    ActionPackageHandoverContext,
    ActionQueryCarbonFootprint,
    ActionQueryEcoAccommodation,
    ActionQueryGreenTransport,
    ActionRankRecommendations,
    ActionTwoStageClarification,
    ValidatePredefinedSlots,
    ValidateTripIntakeForm,
)


class FakeTracker:
    def __init__(self, slots=None, sender_id="test-user", events=None, intent=None):
        self._slots = slots or {}
        self.sender_id = sender_id
        self.events = events or []
        self.latest_message = {"intent": {"name": intent}} if intent else {}

    def get_slot(self, name):
        return self._slots.get(name)


class FakeDispatcher:
    def __init__(self):
        self.messages = []
        self.json_messages = []

    def utter_message(self, text=None, response=None, json_message=None, **kwargs):
        if json_message is not None:
            self.json_messages.append(json_message)
        else:
            self.messages.append(text or response)


def slot_events(events):
    return {e["name"]: e["value"] for e in events if e.get("event") == "slot"}


# --- Carbon footprint -------------------------------------------------------

def test_carbon_footprint_without_api_key_uses_labelled_indicative_figures(monkeypatch):
    monkeypatch.setattr(actions, "CLIMATIQ_API_KEY", "")
    dispatcher = FakeDispatcher()

    events = ActionQueryCarbonFootprint().run(dispatcher, FakeTracker({"destination": "Lisbon"}), {})

    assert "indicative averages" in dispatcher.messages[0]
    card = dispatcher.json_messages[0]
    assert all(e["source"] == "indicative average" for e in card["estimates"])
    # lowest-carbon option is listed first
    assert [e["mode"] for e in card["estimates"]] == ["coach", "train", "flight"]
    assert slot_events(events)["carbon_estimates"] == card


def test_carbon_footprint_handles_api_failure(monkeypatch):
    monkeypatch.setattr(actions, "CLIMATIQ_API_KEY", "fake-key")
    dispatcher = FakeDispatcher()
    with patch("actions.actions.requests.post",
               side_effect=requests.exceptions.RequestException("timeout")):
        ActionQueryCarbonFootprint().run(dispatcher, FakeTracker({"destination": "Kyoto"}), {})

    assert "unavailable right now" in dispatcher.messages[0]
    assert all(e["source"] == "indicative average" for e in dispatcher.json_messages[0]["estimates"])


def test_carbon_footprint_marks_live_figures(monkeypatch):
    monkeypatch.setattr(actions, "CLIMATIQ_API_KEY", "fake-key")
    ok = MagicMock()
    ok.json.return_value = {"co2e": 42.0}
    dispatcher = FakeDispatcher()
    with patch("actions.actions.requests.post", return_value=ok):
        ActionQueryCarbonFootprint().run(dispatcher, FakeTracker({"destination": "Kyoto"}), {})

    assert "Live figures from Climatiq" in dispatcher.messages[0]
    assert all(e["source"] == "Climatiq (live)" for e in dispatcher.json_messages[0]["estimates"])


# --- Accommodation & ranking ------------------------------------------------

def test_eco_accommodation_missing_destination_prompts_user():
    dispatcher = FakeDispatcher()
    events = ActionQueryEcoAccommodation().run(dispatcher, FakeTracker(), {})
    assert events == []
    assert "which destination" in dispatcher.messages[0].lower()


def test_eco_accommodation_curated_results_name_certifier_and_source(monkeypatch):
    monkeypatch.setattr(actions, "AMADEUS_CLIENT_ID", "")
    dispatcher = FakeDispatcher()

    events = ActionQueryEcoAccommodation().run(dispatcher, FakeTracker({"destination": "lisbon"}), {})

    text = dispatcher.messages[0]
    assert "curated list" in text
    assert "certified by Green Key" in text
    assert len(slot_events(events)["eco_hotel_options"]) == 2


def test_eco_accommodation_live_listings_are_flagged_unverified(monkeypatch):
    monkeypatch.setattr(actions, "AMADEUS_CLIENT_ID", "id")
    monkeypatch.setattr(actions, "AMADEUS_CLIENT_SECRET", "secret")
    monkeypatch.setattr(ActionQueryEcoAccommodation, "_token", None)
    token = MagicMock()
    token.json.return_value = {"access_token": "t", "expires_in": 1799}
    hotels = MagicMock()
    hotels.json.return_value = {"data": [{"name": "HOTEL CENTRAL LISBOA"}]}
    dispatcher = FakeDispatcher()
    with patch("actions.actions.requests.post", return_value=token), \
         patch("actions.actions.requests.get", return_value=hotels) as get:
        ActionQueryEcoAccommodation().run(dispatcher, FakeTracker({"destination": "Lisbon"}), {})

    assert get.call_args.kwargs["params"] == {"cityCode": "LIS"}
    assert "not verified" in dispatcher.messages[0]
    assert "Hotel Central Lisboa" in dispatcher.messages[0]


def test_rank_recommendations_applies_sustainability_weighting():
    options = [
        {"name": "Hotel A", "carbon_score": 0.1, "price_score": 0.8},
        {"name": "Hotel B", "carbon_score": 0.8, "price_score": 0.1},
    ]
    tracker = FakeTracker({"eco_hotel_options": options, "sustainability_level": "high"})

    events = ActionRankRecommendations().run(FakeDispatcher(), tracker, {})

    assert slot_events(events)["ranked_options"][0]["name"] == "Hotel A"


def test_rank_recommendations_never_scores_unverified_listings():
    options = [{"name": "Live Hotel", "eco_certified": False, "source": "amadeus"}]
    dispatcher = FakeDispatcher()

    events = ActionRankRecommendations().run(dispatcher, FakeTracker({"eco_hotel_options": options}), {})

    assert events == []
    assert "can't rank" in dispatcher.messages[0]


# --- Transport --------------------------------------------------------------

def test_green_transport_uses_curated_dataset():
    dispatcher = FakeDispatcher()
    ActionQueryGreenTransport().run(dispatcher, FakeTracker({"destination": "Iceland"}), {})
    assert "curated list" in dispatcher.messages[0]
    assert "no railway" in dispatcher.messages[0]


# --- Error recovery & handover ---------------------------------------------

def test_first_misunderstanding_clarifies():
    dispatcher = FakeDispatcher()
    events = ActionTwoStageClarification().run(dispatcher, FakeTracker({"clarification_attempts": 0}), {})
    assert dispatcher.messages == ["utter_clarify_intent"]
    assert slot_events(events) == {"clarification_attempts": 1, "handover_requested": False}


def test_second_consecutive_misunderstanding_escalates():
    dispatcher = FakeDispatcher()
    events = ActionTwoStageClarification().run(dispatcher, FakeTracker({"clarification_attempts": 1}), {})
    assert "human advisor" in dispatcher.messages[0]
    assert slot_events(events) == {"clarification_attempts": 0, "handover_requested": True}


def test_clarification_counter_resets_after_understood_message():
    tracker = FakeTracker({"clarification_attempts": 1}, intent="ask_eco_hotels")
    result = ValidatePredefinedSlots().extract_clarification_attempts(FakeDispatcher(), tracker, {})
    assert result == {"clarification_attempts": 0}

    tracker = FakeTracker({"clarification_attempts": 1}, intent="nlu_fallback")
    assert ValidatePredefinedSlots().extract_clarification_attempts(FakeDispatcher(), tracker, {}) == {}


def test_handover_payload_keeps_last_20_turns_and_sets_flag(caplog):
    events = [{"event": "user", "text": f"msg {i}"} for i in range(30)]
    tracker = FakeTracker({"destination": "Costa Rica"}, events=events)

    with caplog.at_level("DEBUG", logger="actions.actions"):
        result = ActionPackageHandoverContext().run(FakeDispatcher(), tracker, {})

    assert slot_events(result) == {"handover_requested": True}
    assert "(20 turns)" in caplog.text
    assert "msg 9\"" not in caplog.text and "msg 10" in caplog.text


def test_recommendations_pause_after_handover():
    dispatcher = FakeDispatcher()
    events = ActionQueryEcoAccommodation().run(
        dispatcher, FakeTracker({"destination": "Lisbon", "handover_requested": True}), {})
    assert events == []
    assert "human advisor is now handling" in dispatcher.messages[0]


# --- Trip intake form validation -------------------------------------------

def test_budget_validation_parses_free_text():
    form = ValidateTripIntakeForm()
    assert form.validate_budget("about 2,000 dollars", FakeDispatcher(), FakeTracker(), {}) == {"budget": 2000}
    dispatcher = FakeDispatcher()
    assert form.validate_budget("not sure", dispatcher, FakeTracker(), {}) == {"budget": None}
    assert "number" in dispatcher.messages[0]
