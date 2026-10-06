"""
Custom actions for the EcoTrip Advisor Rasa assistant.

  1. action_geocode_location          -> resolves free-text destination to coordinates (OpenCage)
  2. action_query_carbon_footprint    -> per-transport-mode emissions via Climatiq API
  3. action_query_eco_accommodation   -> hotel search via Amadeus sandbox + curated eco-certification list
  4. action_query_green_transport     -> curated low-carbon local transport dataset
  5. action_query_cultural_activities -> curated static dataset (no reliable free API exists)
  6. action_rank_recommendations      -> weighted scoring: carbon impact + price + user preference
  7. action_two_stage_clarification   -> clarify with quick replies once, escalate on the second miss
  8. action_package_handover_context  -> packages conversation context for a human advisor
  +  action_validate_slot_mappings    -> resets the clarification counter after any understood turn
  +  validate_trip_intake_form        -> normalises answers collected by the trip intake form

Every external call is bounded by a 2.5s budget and falls back to clearly
labelled curated/indicative data, so the user always knows whether a figure is
live or not (anti-greenwashing requirement).
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Text, Tuple

import requests
from rasa_sdk import Action, FormValidationAction, Tracker, ValidationAction
from rasa_sdk.events import EventType, SlotSet
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.types import DomainDict

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configuration — real keys are injected via environment variables (.env),
# never hard-coded. See .env.example. Every key is optional.
# --------------------------------------------------------------------------
CLIMATIQ_API_KEY = os.environ.get("CLIMATIQ_API_KEY", "")
CLIMATIQ_ENDPOINT = "https://api.climatiq.io/data/v1/estimate"
CLIMATIQ_DATA_VERSION = os.environ.get("CLIMATIQ_DATA_VERSION", "^21")

AMADEUS_CLIENT_ID = os.environ.get("AMADEUS_CLIENT_ID", "")
AMADEUS_CLIENT_SECRET = os.environ.get("AMADEUS_CLIENT_SECRET", "")
AMADEUS_TOKEN_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADEUS_HOTELS_BY_CITY_URL = "https://test.api.amadeus.com/v1/reference-data/locations/hotels/by-city"
AMADEUS_HOTELS_BY_GEOCODE_URL = "https://test.api.amadeus.com/v1/reference-data/locations/hotels/by-geocode"

OPENCAGE_API_KEY = os.environ.get("OPENCAGE_API_KEY", "")
OPENCAGE_ENDPOINT = "https://api.opencagedata.com/geocode/v1/json"

REQUEST_TIMEOUT = 2.5  # seconds — total budget per action, inside the 3s response target

MOCK_DATA_DIR = Path(__file__).resolve().parent.parent / "mock_data"
MAX_CLARIFICATION_ATTEMPTS = 2  # the 2nd consecutive misunderstanding escalates

# IATA city codes used by Amadeus "hotels by city". Destinations not listed here
# are searched by coordinates (if geocoded) or served from the curated list.
CITY_CODES = {
    "lisbon": "LIS", "kyoto": "UKY", "reykjavik": "REK", "iceland": "REK",
    "costa rica": "SJO", "san jose": "SJO", "bali": "DPS", "cape town": "CPT",
    "barcelona": "BCN", "amsterdam": "AMS", "copenhagen": "CPH", "vienna": "VIE",
    "berlin": "BER", "edinburgh": "EDI", "london": "LON", "paris": "PAR",
}

# Climatiq activity IDs (passenger-km factors). Rail and coach IDs are taken
# from Climatiq's data explorer; override any of them via env if Climatiq
# rejects one — a rejected mode falls back to its indicative factor below.
CLIMATIQ_ACTIVITY_IDS = {
    "flight": os.environ.get(
        "CLIMATIQ_FLIGHT_ACTIVITY_ID",
        "passenger_flight-route_type_international-aircraft_type_na-distance_short_haul_lt_3700km"
        "-class_economy-rf_included-distance_uplift_included",
    ),
    "train": os.environ.get(
        "CLIMATIQ_TRAIN_ACTIVITY_ID", "passenger_train-route_type_national_rail-fuel_source_na"
    ),
    "coach": os.environ.get(
        "CLIMATIQ_COACH_ACTIVITY_ID",
        "passenger_vehicle-vehicle_type_coach-fuel_source_na-distance_na-engine_size_na",
    ),
}

# Indicative kg CO2e per passenger-km (UK DEFRA/BEIS-style averages, rounded).
INDICATIVE_FACTORS = {"flight": 0.15, "train": 0.04, "coach": 0.03}
MODE_ICONS = {"flight": "✈️", "train": "🚆", "coach": "🚌"}

HANDED_OVER_MESSAGE = (
    "A human advisor is now handling your trip, so I've paused automated "
    "recommendations — they'll pick up from here."
)


@lru_cache(maxsize=None)
def _load_mock(filename: Text) -> Tuple[Dict[str, Any], ...]:
    """Load a curated static JSON dataset used where no reliable free API exists
    (e.g. verified eco-certification listings). Cached: the files are read-only."""
    path = MOCK_DATA_DIR / filename
    try:
        with open(path, "r", encoding="utf-8") as f:
            return tuple(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.error("Failed to load mock dataset %s: %s", filename, exc)
        return ()


def _for_destination(filename: Text, destination: Optional[Text]) -> List[Dict[str, Any]]:
    if not destination:
        return []
    wanted = destination.strip().lower()
    return [dict(row) for row in _load_mock(filename)
            if row.get("destination", "").lower() == wanted]


def _handed_over(tracker: Tracker, dispatcher: CollectingDispatcher) -> bool:
    """After a handover, automated recommendation actions stay silent."""
    if tracker.get_slot("handover_requested"):
        dispatcher.utter_message(text=HANDED_OVER_MESSAGE)
        return True
    return False


class ActionGeocodeLocation(Action):
    """Resolves a free-text destination into coordinates so downstream actions
    can query location-aware APIs. Silent on failure: the trip still continues."""

    def name(self) -> Text:
        return "action_geocode_location"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: DomainDict) -> List[EventType]:
        destination = tracker.get_slot("destination")
        if not destination or not OPENCAGE_API_KEY:
            return []

        try:
            resp = requests.get(
                OPENCAGE_ENDPOINT,
                params={"q": destination, "key": OPENCAGE_API_KEY, "limit": 1, "no_annotations": 1},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except requests.exceptions.RequestException as exc:
            logger.warning("OpenCage geocoding failed: %s", exc)
            return []

        if not results:
            return []
        top = results[0]
        dispatcher.utter_message(text=f"📍 Located {top.get('formatted', destination)}.")
        return [
            SlotSet("destination_lat", top["geometry"]["lat"]),
            SlotSet("destination_lng", top["geometry"]["lng"]),
        ]


class ActionQueryCarbonFootprint(Action):
    """Estimates one-way emissions per passenger for flight, train and coach.

    Live figures come from Climatiq; any mode that can't be fetched within the
    time budget uses a clearly labelled indicative factor instead."""

    def name(self) -> Text:
        return "action_query_carbon_footprint"

    # We don't know the traveller's origin, so a representative distance is used
    # and stated explicitly to the user (uncertainty transparency).
    DEFAULT_DISTANCE_KM = 1500

    def _fetch_mode(self, activity_id: Text, distance_km: float) -> Optional[float]:
        resp = requests.post(
            CLIMATIQ_ENDPOINT,
            headers={"Authorization": f"Bearer {CLIMATIQ_API_KEY}"},
            json={
                "emission_factor": {"activity_id": activity_id, "data_version": CLIMATIQ_DATA_VERSION},
                "parameters": {"passengers": 1, "distance": distance_km, "distance_unit": "km"},
            },
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("co2e")

    def _live_estimates(self, distance_km: float) -> Dict[Text, float]:
        """Query all modes in parallel; return only the ones that succeeded in time."""
        live: Dict[Text, float] = {}
        pool = ThreadPoolExecutor(max_workers=len(CLIMATIQ_ACTIVITY_IDS))
        futures = {pool.submit(self._fetch_mode, activity_id, distance_km): mode
                   for mode, activity_id in CLIMATIQ_ACTIVITY_IDS.items()}
        done, _ = wait(futures, timeout=REQUEST_TIMEOUT)
        pool.shutdown(wait=False, cancel_futures=True)
        for future in done:
            mode = futures[future]
            try:
                co2e = future.result()
                if co2e is not None:
                    live[mode] = float(co2e)
            except requests.exceptions.RequestException as exc:
                logger.warning("Climatiq estimate for %s failed: %s", mode, exc)
        return live

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: DomainDict) -> List[EventType]:
        if _handed_over(tracker, dispatcher):
            return []
        destination = tracker.get_slot("destination") or "your destination"
        distance = self.DEFAULT_DISTANCE_KM

        live = self._live_estimates(distance) if CLIMATIQ_API_KEY else {}
        estimates = []
        for mode, factor in INDICATIVE_FACTORS.items():
            if mode in live:
                estimates.append({"mode": mode, "kg_co2e": round(live[mode], 1), "source": "Climatiq (live)"})
            else:
                estimates.append({"mode": mode, "kg_co2e": round(factor * distance, 1),
                                  "source": "indicative average"})
        estimates.sort(key=lambda e: e["kg_co2e"])  # lowest-carbon option first

        if not live:
            reason = "no Climatiq API key configured" if not CLIMATIQ_API_KEY else \
                     "the carbon calculator is unavailable right now"
            header = f"I couldn't get live figures ({reason}), so these are indicative averages."
        elif len(live) < len(INDICATIVE_FACTORS):
            header = "Some live figures were unavailable; those rows use indicative averages."
        else:
            header = "Live figures from Climatiq."

        lines = [
            f"Estimated one-way emissions per passenger to {destination}, assuming ~{distance:,} km "
            f"(I don't know your starting point). {header}"
        ]
        for e in estimates:
            lines.append(f"{MODE_ICONS[e['mode']]} {e['mode'].title()}: ~{e['kg_co2e']:.0f} kg CO2e ({e['source']})")
        by_mode = {e["mode"]: e["kg_co2e"] for e in estimates}
        if by_mode["flight"] > 0:
            share = by_mode["train"] / by_mode["flight"]
            lines.append(f"The train emits about {share:.0%} of the flight's emissions over the same distance.")

        dispatcher.utter_message(text="\n".join(lines))
        card = {"type": "carbon_estimates", "destination": destination,
                "distance_km": distance, "estimates": estimates}
        dispatcher.utter_message(json_message=card)
        return [SlotSet("carbon_estimates", card)]


class ActionQueryEcoAccommodation(Action):
    """Lists accommodation for the destination.

    Eco-certification comes only from the curated dataset (the Amadeus sandbox
    has no certification data). Live Amadeus listings are shown as
    "not verified" so nothing is called green without a named certifier."""

    def name(self) -> Text:
        return "action_query_eco_accommodation"

    _token: Optional[Text] = None
    _token_expiry: float = 0.0

    @classmethod
    def _get_amadeus_token(cls, timeout: float) -> Text:
        if cls._token and time.time() < cls._token_expiry:
            return cls._token
        resp = requests.post(
            AMADEUS_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": AMADEUS_CLIENT_ID,
                "client_secret": AMADEUS_CLIENT_SECRET,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        body = resp.json()
        cls._token = body["access_token"]
        cls._token_expiry = time.time() + int(body.get("expires_in", 1799)) - 60
        return cls._token

    def _live_hotels(self, tracker: Tracker, destination: Text) -> Optional[List[Text]]:
        """Return live hotel names, or None if no live search was possible."""
        if not AMADEUS_CLIENT_ID or not AMADEUS_CLIENT_SECRET:
            return None
        city_code = CITY_CODES.get(destination.strip().lower())
        lat, lng = tracker.get_slot("destination_lat"), tracker.get_slot("destination_lng")
        if city_code:
            url, params = AMADEUS_HOTELS_BY_CITY_URL, {"cityCode": city_code}
        elif lat is not None and lng is not None:
            url, params = AMADEUS_HOTELS_BY_GEOCODE_URL, {"latitude": lat, "longitude": lng, "radius": 10}
        else:
            return None

        deadline = time.monotonic() + REQUEST_TIMEOUT
        try:
            token = self._get_amadeus_token(timeout=REQUEST_TIMEOUT)
            remaining = max(0.5, deadline - time.monotonic())
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                                params=params, timeout=remaining)
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.warning("Amadeus hotel search failed: %s", exc)
            return None
        return [h.get("name", "").title() for h in resp.json().get("data", []) if h.get("name")]

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: DomainDict) -> List[EventType]:
        if _handed_over(tracker, dispatcher):
            return []
        destination = tracker.get_slot("destination")
        if not destination:
            dispatcher.utter_message(text="Which destination would you like hotel options for?")
            return []

        curated = [{**h, "source": "curated"} for h in _for_destination("eco_hotels.json", destination)]
        live_names = self._live_hotels(tracker, destination)
        curated_names = {h["name"].lower() for h in curated}
        live = [{"name": n, "destination": destination, "eco_certified": False, "source": "amadeus"}
                for n in (live_names or []) if n.lower() not in curated_names][:3]

        options = curated + live
        if not options:
            dispatcher.utter_message(
                text=f"I don't have verified eco-certified hotels for {destination} yet. "
                     f"A human advisor can source options — just ask to talk to one."
            )
            return [SlotSet("eco_hotel_options", [])]

        lines = []
        if curated:
            lines.append(f"Eco-certified places to stay in {destination} "
                         f"(from our curated list, not live availability):")
            lines += [f"- 🌿 {h['name']} — certified by {h['certification']}" for h in curated]
        else:
            lines.append(f"I have no verified eco-certified hotels for {destination} yet.")
        if live:
            lines.append("Other hotels from the live Amadeus listing (⚠️ eco-certification not verified):")
            lines += [f"- {h['name']}" for h in live]
        elif live_names is None and (AMADEUS_CLIENT_ID and AMADEUS_CLIENT_SECRET):
            lines.append("(The live hotel search was unavailable just now.)")

        dispatcher.utter_message(text="\n".join(lines))
        return [SlotSet("eco_hotel_options", options)]


class ActionQueryGreenTransport(Action):
    """Low-carbon local transport. No free API reliably covers this per
    destination, so a curated dataset is used and labelled as such."""

    def name(self) -> Text:
        return "action_query_green_transport"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: DomainDict) -> List[EventType]:
        if _handed_over(tracker, dispatcher):
            return []
        destination = tracker.get_slot("destination")
        rows = _for_destination("green_transport.json", destination)
        if not rows:
            place = destination or "most destinations"
            dispatcher.utter_message(
                text=f"I don't have curated transport details for {place} yet. As a rule, "
                     f"walking, cycling and public transport are the lowest-carbon ways to get "
                     f"around; a human advisor can confirm current local options."
            )
            return []

        lines = [f"Low-carbon ways to get around {destination} (curated list, may change — check locally):"]
        lines += [f"- {o}" for o in rows[0]["options"]]
        dispatcher.utter_message(text="\n".join(lines))
        return []


class ActionQueryCulturalActivities(Action):
    """No reliable free API exists for verified community-based tourism
    activities, so this serves a curated dataset — flagged to the user."""

    def name(self) -> Text:
        return "action_query_cultural_activities"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: DomainDict) -> List[EventType]:
        if _handed_over(tracker, dispatcher):
            return []
        destination = tracker.get_slot("destination")
        activities = _for_destination("cultural_activities.json", destination)
        if not activities:
            dispatcher.utter_message(
                text=f"I don't yet have verified community-based activities listed for "
                     f"{destination or 'that destination'}. A human advisor can research "
                     f"current options for you."
            )
            return []

        lines = [f"Sustainable activities in {destination} (curated list, reviewed quarterly):"]
        lines += [f"- {a['name']}: {a['description']}" for a in activities[:5]]
        dispatcher.utter_message(text="\n".join(lines))
        return []


class ActionRankRecommendations(Action):
    """Ranks accommodation by a transparent weighted score:

        score = w_carbon * (1 - carbon_score) + w_price * (1 - price_score)

    Weights follow the user's sustainability_level. Only options with real
    carbon/price scores are ranked — unverified listings are never scored."""

    def name(self) -> Text:
        return "action_rank_recommendations"

    WEIGHTS = {
        "high":   {"carbon": 0.7, "price": 0.3},
        "medium": {"carbon": 0.5, "price": 0.5},
        "low":    {"carbon": 0.3, "price": 0.7},
    }

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: DomainDict) -> List[EventType]:
        if _handed_over(tracker, dispatcher):
            return []
        options = tracker.get_slot("eco_hotel_options") or []
        preference = tracker.get_slot("sustainability_level") or "medium"
        weights = self.WEIGHTS.get(preference, self.WEIGHTS["medium"])

        scorable = [o for o in options
                    if o.get("carbon_score") is not None and o.get("price_score") is not None]
        if not scorable:
            if options:
                dispatcher.utter_message(
                    text="I can't rank these options because I don't have verified carbon "
                         "and price data for them."
                )
            return []

        scored = []
        for option in scorable:
            weighted = (weights["carbon"] * (1 - option["carbon_score"]) +
                        weights["price"] * (1 - option["price_score"]))
            scored.append({**option, "weighted_score": round(weighted, 3)})
        scored.sort(key=lambda o: o["weighted_score"], reverse=True)

        lines = [f"Ranked for your '{preference}' sustainability priority "
                 f"(weights: carbon {weights['carbon']:.0%}, price {weights['price']:.0%}):"]
        for rank, o in enumerate(scored[:3], start=1):
            lines.append(f"{rank}. {o['name']} — score {o['weighted_score']}")
        dispatcher.utter_message(text="\n".join(lines))
        return [SlotSet("ranked_options", scored)]


class ActionTwoStageClarification(Action):
    """Error recovery: the first misunderstanding re-prompts with constrained
    quick replies; a second consecutive one escalates to a human advisor
    (rules then run action_package_handover_context)."""

    def name(self) -> Text:
        return "action_two_stage_clarification"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: DomainDict) -> List[EventType]:
        attempts = int(tracker.get_slot("clarification_attempts") or 0) + 1

        if attempts < MAX_CLARIFICATION_ATTEMPTS:
            dispatcher.utter_message(response="utter_clarify_intent")
            return [SlotSet("clarification_attempts", attempts),
                    SlotSet("handover_requested", False)]

        dispatcher.utter_message(
            text="I've had trouble understanding a couple of times now — "
                 "let's get you to a human advisor instead."
        )
        return [SlotSet("clarification_attempts", 0), SlotSet("handover_requested", True)]


class ActionPackageHandoverContext(Action):
    """Packages slots, ranked options and the last 20 turns into a structured
    payload for a human advisor."""

    def name(self) -> Text:
        return "action_package_handover_context"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker,
            domain: DomainDict) -> List[EventType]:
        transcript = [
            {"sender": e.get("event"), "text": e.get("text")}
            for e in tracker.events
            if e.get("event") in ("user", "bot") and e.get("text")
        ][-20:]  # last 20 turns is enough context without over-sharing history

        handover_payload = {
            "conversation_id": tracker.sender_id,
            "collected_preferences": {
                "destination": tracker.get_slot("destination"),
                "travel_dates": tracker.get_slot("travel_dates"),
                "budget": tracker.get_slot("budget"),
                "sustainability_level": tracker.get_slot("sustainability_level"),
            },
            "ranked_options": tracker.get_slot("ranked_options"),
            "transcript_tail": transcript,
        }

        # In production this would POST to the advisor CRM/queue. Here the
        # transcript is only emitted at DEBUG so normal logs hold no
        # conversation content (GDPR data minimisation).
        logger.info("Handover packaged for conversation %s (%d turns)",
                    tracker.sender_id, len(transcript))
        logger.debug("Handover payload: %s", json.dumps(handover_payload, default=str))

        return [SlotSet("handover_requested", True)]


class ValidatePredefinedSlots(ValidationAction):
    """Runs after every user message. Resets the clarification counter as soon
    as a message is understood, so only *consecutive* misses escalate."""

    def extract_clarification_attempts(self, dispatcher: CollectingDispatcher,
                                       tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        intent = (tracker.latest_message.get("intent") or {}).get("name")
        if intent != "nlu_fallback" and tracker.get_slot("clarification_attempts"):
            return {"clarification_attempts": 0}
        return {}


class ValidateTripIntakeForm(FormValidationAction):
    """Normalises the answers collected by trip_intake_form."""

    def name(self) -> Text:
        return "validate_trip_intake_form"

    def validate_destination(self, value: Any, dispatcher: CollectingDispatcher,
                             tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        value = str(value).strip() if value else ""
        if len(value) < 2:
            dispatcher.utter_message(text="Sorry, which destination did you mean?")
            return {"destination": None}
        return {"destination": value if not value.islower() else value.title()}

    def validate_travel_dates(self, value: Any, dispatcher: CollectingDispatcher,
                              tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        value = str(value).strip() if value else ""
        return {"travel_dates": value or None}

    def validate_budget(self, value: Any, dispatcher: CollectingDispatcher,
                        tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        match = re.search(r"\d[\d,]*(?:\.\d+)?", str(value or ""))
        amount = float(match.group().replace(",", "")) if match else 0.0
        if amount <= 0:
            dispatcher.utter_message(text="Please give the budget as a number, e.g. 1500.")
            return {"budget": None}
        return {"budget": int(amount) if amount.is_integer() else amount}

    def validate_sustainability_level(self, value: Any, dispatcher: CollectingDispatcher,
                                      tracker: Tracker, domain: DomainDict) -> Dict[Text, Any]:
        value = str(value or "").lower()
        if value not in ("low", "medium", "high"):
            return {"sustainability_level": None}
        return {"sustainability_level": value}
