"""Rules first, and every decision can be explained. The rules decide everything, there's no AI in this file."""
import math  # for the distance maths
from dataclasses import dataclass, field  # shortcuts for making simple data-holder classes
from datetime import datetime  # type hint for times
from .tables import Donation, Organisation  # the two kinds of things we compare
from .safe_window import slack_minutes  # "how much spare time is there?" helper

# How much each factor counts toward a recipient's score (they add up to 1.0 = 100%).
WEIGHTS = {"time_margin": 0.30, "storage_fit": 0.20, "load_fit": 0.20, "fair_share": 0.15, "reliability": 0.15}
# 30 spare minutes counts as a "full marks" time margin.
TARGET_SLACK_MIN = 30
# After 7 days without food, a recipient gets the maximum fair-share points.
FAIR_SHARE_CAP_DAYS = 7
# Recipients whose scores are within 5 points of the top one count as "about equal".
FAIR_SHARE_BAND = 5.0
# Assumed driving speed for travel estimates.
SPEED_KMPH = 25.0


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    # Straight-line distance between two GPS points on the globe (the "haversine" formula).
    # Convert the two latitudes from degrees to radians, which the maths needs.
    lat1_rad, lat2_rad = math.radians(lat1), math.radians(lat2)
    # The differences in latitude and longitude, also in radians.
    delta_lat, delta_lon = lat2_rad - lat1_rad, math.radians(lon2 - lon1)
    # The core of the formula.
    haversine_a = math.sin(delta_lat / 2) ** 2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(delta_lon / 2) ** 2
    # 6371 is Earth's radius in km; this turns the formula result into kilometres.
    return 6371 * 2 * math.asin(math.sqrt(haversine_a))


def eta_minutes(donor: Organisation, recipient: Organisation) -> float:
    # Distance divided by speed gives hours, times 60 gives minutes.
    return haversine_km(donor.lat, donor.lon, recipient.lat, recipient.lon) / SPEED_KMPH * 60


def combine(factors: dict, weights: dict = WEIGHTS) -> float:
    # Multiply each factor by its weight, add them up, and round to 2 decimals.
    return round(sum(weights[k] * factors[k] for k in weights), 2)


def smoothed_reliability(completed: int, attempts: int) -> float:
    # Reliability as a percentage; the +2 and +4 stop a brand-new organisation from starting at 0% or 100% (a new one starts at 50%).
    return 100 * (completed + 2) / (attempts + 4)


def load_fit(quantity: int, capacity: int) -> float:
    # What share of their spare capacity this food would use.
    ratio = quantity / capacity
    # Using 40% or more is a full-marks fit; smaller loads score proportionally lower.
    return 100.0 if ratio >= 0.4 else 100 * ratio / 0.4


@dataclass
class Result:
    # The outcome of checking one recipient against one listing.
    org_id: int  # the recipient's id
    name: str  # the recipient's name
    eligible: bool  # can they take this food at all?
    excluded_reasons: list = field(default_factory=list)  # reason codes if they can't
    factors: dict = field(default_factory=dict)  # the individual scores (time, storage, ...)
    score: float = 0.0  # the combined score
    eta_min: float = 0.0  # estimated travel time
    slack_min: float = 0.0  # spare minutes after travel and buffer
    reasons: list = field(default_factory=list)  # reason codes explaining why they're recommended
    fair_share_applied: bool = False  # did fair-share rotation affect their position?


def evaluate(listing: Donation, donor: Organisation, recipient: Organisation, now: datetime) -> Result:
    # Start by assuming this recipient is eligible; we'll flip that if any rule fails.
    result = Result(recipient.id, recipient.name, True)
    # Turn the comma-separated text lists into real Python lists (the "if c" skips empty pieces).
    accepted_categories = [c for c in recipient.accepts_categories.split(",") if c]
    storage_types = [c for c in recipient.storage_available.split(",") if c]
    # Rule 1: must be approved by the admin and still active.
    if not (recipient.verified and recipient.active):
        result.excluded_reasons.append("NOT_VERIFIED_OR_INACTIVE")
    # Rule 2: must accept this type of food.
    if listing.category not in accepted_categories:
        result.excluded_reasons.append("CATEGORY_NOT_ACCEPTED")
    # Rule 3: must have the right kind of storage.
    if listing.storage not in storage_types:
        result.excluded_reasons.append("STORAGE_REQUIREMENT_NOT_MET")
    # Rule 4: must have enough spare capacity.
    if listing.quantity > recipient.capacity_remaining:
        result.excluded_reasons.append("CAPACITY_EXCEEDED")
    # Work out the travel time and how much spare time is left after it.
    result.eta_min = eta_minutes(donor, recipient)
    result.slack_min = slack_minutes(listing.latest_collect, now, result.eta_min)
    # Rule 5: must be able to arrive before the safe window closes.
    if result.slack_min < 0:
        result.excluded_reasons.append("CANNOT_ARRIVE_IN_SAFE_WINDOW")
    # If any rule failed, mark them ineligible and stop here (no score for excluded recipients).
    if result.excluded_reasons:
        result.eligible = False
        return result
    # Days since they last got food; if never, we treat it as the maximum so they get full fair-share points.
    days = (now - recipient.last_received_at).days if recipient.last_received_at else FAIR_SHARE_CAP_DAYS
    # Score each factor from 0 to 100.
    result.factors = {
        "time_margin": 100 * min(1.0, result.slack_min / TARGET_SLACK_MIN),  # more spare time = better, capped at 100
        "storage_fit": 100.0,  # they passed the storage rule, so full marks
        "load_fit": load_fit(listing.quantity, recipient.capacity_remaining),  # how well the amount fits their space
        "fair_share": 100 * min(days, FAIR_SHARE_CAP_DAYS) / FAIR_SHARE_CAP_DAYS,  # longer wait = more points
        "reliability": smoothed_reliability(recipient.completed, recipient.attempts),  # track record
    }
    # Combine the factors into one score.
    result.score = combine(result.factors)
    # The reasons shown to the user for why they were recommended.
    result.reasons = ["CATEGORY_ACCEPTED", "STORAGE_COMPATIBLE", "CAPACITY_AVAILABLE", "ARRIVES_WITH_TIME_TO_SPARE"]
    return result


def rank(results: list) -> list:
    """Sort the eligible ones by score; inside the fair-share band, whoever got food least recently goes first."""
    # Keep only eligible recipients, highest score first.
    eligible_left = sorted([item for item in results if item.eligible], key=lambda item: -item.score)
    # The final ordered list we're building.
    ranked = []
    # Keep picking the next winner until nobody's left.
    while eligible_left:
        # The best score among those remaining.
        top = eligible_left[0].score
        # Everyone within the fair-share band of the top score counts as "about equal".
        band = [item for item in eligible_left if top - item.score <= FAIR_SHARE_BAND]
        if len(band) > 1:
            # Inside the band, whoever waited longest goes first (ties broken by score).
            band.sort(key=lambda item: (-item.factors["fair_share"], -item.score))
            for item in band:
                # Mark everyone in the band so the explanation can mention fair-share rotation.
                item.fair_share_applied = True
                item.reasons.append("FAIR_SHARE_ROTATION")
        # The first one in the band is next in the final order.
        ranked.append(band[0])
        # Remove that winner from the waiting list and go again.
        eligible_left = [item for item in eligible_left if item is not band[0]]
    return ranked


# Plain-English text for each reason code.
EXPLAIN = {
    "CATEGORY_ACCEPTED": "accepts this category", "STORAGE_COMPATIBLE": "has compatible storage",
    "CAPACITY_AVAILABLE": "has declared capacity", "ARRIVES_WITH_TIME_TO_SPARE": "can arrive within the safe window",
    "FAIR_SHARE_ROTATION": "has been served less recently",
}


def explain(result: Result) -> str:
    # Join the plain-English reasons into one sentence, always ending with the "not a safety certificate" reminder.
    return ("Recommended because it " + ", ".join(EXPLAIN[c] for c in result.reasons)
            + ". This is a coordination recommendation, not a certification that the food is safe.")
