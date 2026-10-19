# The Safe-Window Clock: works out the last moment food can safely be collected.
import json  # to read the rules file
import os  # to read settings and build file paths
from datetime import datetime, timedelta  # datetime = date+time, timedelta = a length of time

# Which rules file to use. Default is the example one next to the app (override with FOODBRIDGE_RULES for a reviewed one).
RULES_PATH = os.getenv("FOODBRIDGE_RULES", os.path.join(os.path.dirname(__file__), "..", "rules.example.json"))
# Extra minutes we always reserve for handing the food over.
HANDLING_BUFFER_MIN = 10


class RuleError(Exception):
    # Raised when a food type has no rule, or the rules aren't allowed to be used.
    pass


def load_rules(path: str = RULES_PATH) -> dict:
    # Open the rules file and read it as JSON.
    with open(path) as rules_file:
        data = json.load(rules_file)
    # If the rules haven't been expert-reviewed, only carry on when the "allow example rules" switch is on (it's on by default for demos).
    if data.get("status") != "REVIEWED" and os.getenv("FOODBRIDGE_ALLOW_EXAMPLE_RULES", "1") != "1":
        raise RuleError("Rule table is not expert-reviewed; refusing to run in this mode.")
    # Turn the list of rules into a lookup table: (food type, storage) -> max minutes it can be held.
    return {(rule["category"], rule["storage"]): rule["max_hold_minutes"] for rule in data["rules"]}


def latest_collect(rules: dict, category: str, storage: str, prepared_at: datetime,
                   donor_deadline: datetime) -> datetime:
    # The lookup key for this food type + storage combo.
    key = (category, storage)
    # No rule (or a rule with no time) means we refuse, never guess.
    if key not in rules or rules[key] is None:
        raise RuleError(f"Category/storage '{category}/{storage}' is not accepted (no reviewed rule).")
    # The safe deadline is whichever comes first: the donor's own deadline, or prepared time + max hold time.
    return min(donor_deadline, prepared_at + timedelta(minutes=rules[key]))


def slack_minutes(latest: datetime, now: datetime, eta_min: float) -> float:
    # Minutes left until the deadline, minus the travel time, minus the handover buffer. Negative = can't make it in time.
    return (latest - now).total_seconds() / 60 - eta_min - HANDLING_BUFFER_MIN
