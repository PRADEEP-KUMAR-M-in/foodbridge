"""A step-by-step simulation of surplus-food coordination: today's "broadcast and wait" way vs FOODBRIDGE.

THIS IS A MODEL, NOT A MEASUREMENT. Every probability and delay below is an ASSUMPTION (see Params).
The results show what the design COULD do if the assumptions hold; only a real pilot can show what it actually does.
The FOODBRIDGE side re-uses the real scoring code in app/matching.py, so the ranking being simulated is the same ranking the app runs.
"""
import math  # exp(), floor(), ceil() and friends
import random  # seeded random numbers (same seed = same results every time)
import statistics  # median
from dataclasses import asdict, dataclass, replace  # easy settings objects; asdict = to dictionary, replace = copy with changes
from . import matching  # the app's real ranking code


@dataclass(frozen=True)
class Params:
    days: int = 7  # days in one simulated week
    donors: int = 3  # how many food donors
    recipients: int = 8  # how many receiving organisations
    listings_per_day: float = 3.0  # average new listings per day
    p_respond: float = 0.70            # chance a recipient replies at all (ghosting is real)
    median_delay_min: float = 20.0     # typical time to reply, in minutes
    delay_sigma: float = 0.8           # how spread out the reply times are (log-normal)
    p_accept: float = 0.80             # says yes when the food really does fit them
    p_blind_yes: float = 0.30          # says yes to a broadcast that does NOT fit them (nobody checked their storage/space)
    p_discover_early: float = 0.50     # chance the mismatch gets caught on the confirmation call (otherwise they find out when they show up: wasted trip)
    confirm_delay_min: float = 5.0     # time for a manual confirmation call/text (not needed with in-app claims)
    manual_wait_min: float = 45.0      # how long a donor waits on a no-show before moving on by hand
    claim_timeout_min: float = 15.0    # FOODBRIDGE auto-expires a claim after this many minutes
    offer_window_min: float = 2.0      # FOODBRIDGE first-dibs time per recipient (same as the app default)
    p_noshow_mean: float = 0.12        # average chance that someone who claimed just never shows up
    handling_buffer_min: float = 10.0  # minutes reserved for the handover itself
    deadline_choices: tuple = (90, 120, 180, 240)   # minutes of safe time left when a listing gets posted


@dataclass(frozen=True)
class Flags:
    filters: bool          # strict filters + score ranking (otherwise everyone gets picked in random order)
    cascade: bool          # first-dibs offers one at a time (otherwise it's a broadcast to everybody at once)
    auto_expiry: bool      # claims nobody confirms expire on their own
    instant_confirm: bool  # claim inside the app in one go (no confirmation call needed)
    fair_share: bool       # fair-share rotation inside the score band (everybody gets a turn)


# "Today": every FOODBRIDGE feature switched off.
BASELINE = Flags(False, False, False, False, False)
# "FOODBRIDGE": every feature switched on.
FULL = Flags(True, True, True, True, True)
# The scenarios we compare: both extremes, plus "FOODBRIDGE with one part removed" to see which part matters.
SCENARIOS = {
    "baseline": BASELINE,
    "full": FULL,
    "no_filters": replace(FULL, filters=False),
    "no_cascade": replace(FULL, cascade=False),
    "no_auto_expiry": replace(FULL, auto_expiry=False),
    "no_instant_confirm": replace(FULL, instant_confirm=False),
    "no_fair_share": replace(FULL, fair_share=False),
}
# Human-readable names for each scenario (shown in the tables and charts).
LABELS = {
    "baseline": "Today: broadcast and wait", "full": "FOODBRIDGE (all parts)", "no_filters": "FOODBRIDGE without constraint filters",
    "no_cascade": "FOODBRIDGE without first-dibs cascade", "no_auto_expiry": "FOODBRIDGE without automatic claim expiry",
    "no_instant_confirm": "FOODBRIDGE without in-app instant claim", "no_fair_share": "FOODBRIDGE without fair-share rotation",
}
# The centre of the made-up city (somewhere around Chennai); everyone is placed randomly near here.
BOX = (13.08, 80.27)


class SimRecipient:
    # One pretend receiving organisation.
    def __init__(self, i, rng):
        # Give it an id and a random spot within about 4 km of the centre.
        self.id, self.lat, self.lon = i, BOX[0] + rng.uniform(-0.04, 0.04), BOX[1] + rng.uniform(-0.04, 0.04)
        # Everyone has room-temperature storage; 40% also have a fridge, 30% also can keep food hot.
        self.storages = {"ambient"} | ({"chilled"} if rng.random() < 0.4 else set()) | ({"hot_held"} if rng.random() < 0.3 else set())
        # How much they can hold per day (30, 60 or 100), and how much is left right now.
        self.cap = rng.choice([30, 60, 100])
        self.cap_left = self.cap
        self.speed = rng.uniform(0.5, 2.0)                       # how fast this person personally replies (multiplier)
        self.noshow = 0.0                                        # gets filled in later by make_env
        # Track record, starting empty.
        self.completed = self.attempts = 0
        # The day they last received food (None = never).
        self.last_day = None
        # Total units they've received.
        self.units = 0


class SimDonation:
    # One pretend listing.
    def __init__(self, day, pos, qty, storage, deadline):
        # Which day it appears, where, how many units, how it's stored, and how many minutes of safe time it has.
        self.day, self.pos, self.qty, self.storage, self.deadline = day, pos, qty, storage, deadline


def _poisson(rate, rng):
    # Picks a random whole number around "rate" (the count of random events in a period), using Knuth's method.
    limit, count, product = math.exp(-rate), 0, 1.0
    while True:
        # Multiply by a random number each round; stop once the product drops below the limit.
        product *= rng.random()
        if product <= limit:
            return count
        count += 1


def make_env(params: Params, seed: int):
    # Build the pretend world: the same seed always builds the exact same world.
    rng = random.Random(seed)
    # Donors are just random locations near the centre.
    donors = [(BOX[0] + rng.uniform(-0.04, 0.04), BOX[1] + rng.uniform(-0.04, 0.04)) for _ in range(params.donors)]
    # Create the receiving organisations.
    recipients = [SimRecipient(i, rng) for i in range(params.recipients)]
    for recipient in recipients:
        # Each one gets their own no-show chance, between 0 and double the average (capped at 90%).
        recipient.noshow = min(0.9, rng.uniform(0, 2 * params.p_noshow_mean))
    donations = []
    # For each day, create a random number of listings.
    for day in range(params.days):
        for _ in range(_poisson(params.listings_per_day, rng)):
            # A random donor, quantity, storage type (ambient most often) and safe-time deadline.
            donations.append(SimDonation(day, rng.choice(donors), rng.choice([10, 20, 30, 50]),
                            rng.choices(["ambient", "chilled", "hot_held"], [0.6, 0.25, 0.15])[0], rng.choice(params.deadline_choices)))
    return recipients, donations


def _eta(donation, recipient):
    # Travel time in minutes from the donor to the recipient.
    return matching.haversine_km(donation.pos[0], donation.pos[1], recipient.lat, recipient.lon) / matching.SPEED_KMPH * 60


def _fits(recipient, donation):
    # Can they take it? They need the right storage AND enough space left today.
    return donation.storage in recipient.storages and recipient.cap_left >= donation.qty


def _feasible(params, donation, recipient, minute):
    # Can they get there in time? (time left - travel - handover buffer must not be negative)
    return donation.deadline - minute - _eta(donation, recipient) - params.handling_buffer_min >= 0


def _delay(params, recipient, rng):
    # A random reply time: the typical delay, scaled by how fast this person is, and stretched by a bell-curve-in-the-log factor.
    return params.median_delay_min * recipient.speed * math.exp(params.delay_sigma * rng.gauss(0, 1))


def _yes(params, recipient, donation, rng):
    # Rolls the dice on whether they say yes: likely if it fits them, less likely if it doesn't.
    return rng.random() < (params.p_accept if _fits(recipient, donation) else params.p_blind_yes)


def _pick(params, flags, pool, donation, minute, rng):
    # Without filters, the donor just picks any organisation at random.
    if not flags.filters:
        return rng.choice(pool)
    # With filters we score everyone using the app's real scoring formulas.
    scored = []
    for recipient in pool:
        # Spare minutes after travel and the buffer.
        slack = donation.deadline - minute - _eta(donation, recipient) - params.handling_buffer_min
        # Days since they last got food (the maximum if never).
        days = (donation.day - recipient.last_day) if recipient.last_day is not None else matching.FAIR_SHARE_CAP_DAYS
        # The same five factors matching.evaluate() uses.
        factors = {"time_margin": 100 * min(1.0, slack / matching.TARGET_SLACK_MIN), "storage_fit": 100.0,
             "load_fit": matching.load_fit(donation.qty, recipient.cap_left),
             "fair_share": 100 * min(days, matching.FAIR_SHARE_CAP_DAYS) / matching.FAIR_SHARE_CAP_DAYS,
             "reliability": matching.smoothed_reliability(recipient.completed, recipient.attempts)}
        # Remember (score, fair-share points, who).
        scored.append((matching.combine(factors), factors["fair_share"], recipient))
    # The best score.
    top = max(s[0] for s in scored)
    if flags.fair_share:
        # Everyone within the fair-share band of the top score; the one who waited longest wins (ties go to the higher score).
        band = [s for s in scored if top - s[0] <= matching.FAIR_SHARE_BAND]
        return max(band, key=lambda s: (s[1], s[0]))[2]
    # Without fair-share, the highest score wins outright.
    return max(scored, key=lambda s: s[0])[2]


def _attempt(params, flags, recipient, donation, claim_minute, rng):
    """What happens once a recipient is holding a claim at claim_minute. Gives back (kind, time_after)."""
    # How long the recipient needs to travel to the donor.
    travel_min = _eta(donation, recipient)
    if not _fits(recipient, donation):
        # The food doesn't fit them. Either someone notices early (rejected, no trip), or they only find out on arrival (wasted trip).
        if rng.random() < params.p_discover_early:
            return "rejected", claim_minute
        return "wasted", claim_minute + travel_min                         # drove all the way there and couldn't even take it, ouch
    if flags.filters and not _feasible(params, donation, recipient, claim_minute):
        return "rejected", claim_minute                           # the app checks the clock again when you claim
    # They really are going to try, so it counts as an attempt.
    recipient.attempts += 1
    if rng.random() < recipient.noshow:
        # They claimed it and then never showed up.
        return "noshow", claim_minute
    # The minute they'd arrive.
    arrival_min = claim_minute + travel_min
    if arrival_min > donation.deadline:
        return "wasted", arrival_min                                 # showed up after the safe window closed (too late)
    # Everything worked.
    return "delivered", arrival_min


def _after_failure(params, flags, kind, claim_minute, minute_after):
    if kind == "noshow":
        # After a no-show, the app moves on after the claim timeout; the manual way waits much longer before noticing.
        return claim_minute + (params.claim_timeout_min if flags.auto_expiry else params.manual_wait_min)
    # After a wasted trip, time moves on to when they got back; any other failure costs no extra time.
    return minute_after if kind == "wasted" else claim_minute


def _deliver(recipient, donation, claim_minute):
    # Update the recipient's books after a successful delivery: less room left, a better record, last-received day, and total units.
    recipient.cap_left -= donation.qty
    recipient.completed += 1
    recipient.last_day = donation.day
    recipient.units += donation.qty
    return claim_minute


def _handle(params, flags, recipients, donation, rng):
    """Gives back (claim_time or None, wasted_trips)."""
    # In-app claims need no confirmation call; the manual way needs one.
    confirm = 0.0 if flags.instant_confirm else params.confirm_delay_min
    # Wasted trips so far.
    wasted = 0
    if not flags.cascade:                                        # broadcast to everyone at once
        # With filters, only organisations that fit and can make it in time even hear about it.
        pool = [recipient for recipient in recipients if (not flags.filters) or (_fits(recipient, donation) and _feasible(params, donation, recipient, 0))]
        # Everyone who replies "yes", with how long it took them, sorted so the fastest "yes" comes first.
        yeses = sorted(((_delay(params, recipient, rng), recipient) for recipient in pool if rng.random() < params.p_respond and _yes(params, recipient, donation, rng)), key=lambda x: x[0])
        # The earliest time the donor is free to deal with the next "yes".
        cursor = 0.0
        for reply_delay, recipient in yeses:
            # When this claim happens: after their reply, and after the donor is free, plus any confirmation call.
            claim_minute = max(reply_delay, cursor) + confirm
            if claim_minute >= donation.deadline:
                # Too late to matter, and so is everyone after them.
                break
            kind, minute_after = _attempt(params, flags, recipient, donation, claim_minute, rng)
            if kind == "delivered":
                return _deliver(recipient, donation, claim_minute), wasted
            # A wasted trip is counted (True counts as 1).
            wasted += kind == "wasted"
            # The donor can move on to the next "yes" after this failure.
            cursor = _after_failure(params, flags, kind, claim_minute, minute_after)
        # Nobody delivered.
        return None, wasted
    # First-dibs, one recipient at a time: the current minute, who's had a turn, and the "late replies" that came after a turn ended.
    minute, tried, late = 0.0, set(), []
    while minute < donation.deadline:
        # Organisations that haven't had a turn yet.
        pool = [recipient for recipient in recipients if recipient.id not in tried]
        if flags.filters:
            # Drop the ones who can't take it or can't make it in time.
            pool = [recipient for recipient in pool if _fits(recipient, donation) and _feasible(params, donation, recipient, minute)]
        if not pool:
            # Nobody left to offer it to.
            break
        # Choose who gets this turn, and mark them as having had it.
        recipient = _pick(params, flags, pool, donation, minute, rng)
        tried.add(recipient.id)
        # Do they say yes at all, and how long would they take to answer?
        yes = rng.random() < params.p_respond and _yes(params, recipient, donation, rng)
        reply_delay = _delay(params, recipient, rng)
        if yes and reply_delay <= params.offer_window_min:
            # They answered inside their window: they claim it.
            claim_minute = minute + reply_delay + confirm
            kind, minute_after = _attempt(params, flags, recipient, donation, claim_minute, rng)
            if kind == "delivered":
                return _deliver(recipient, donation, claim_minute), wasted
            wasted += kind == "wasted"
            # The offer carries on from after the failure.
            minute = _after_failure(params, flags, kind, claim_minute, minute_after)
            continue
        if yes:
            late.append((minute + reply_delay, recipient))                         # replied after their window had already closed
        # Their turn is over; the next person gets the next window.
        minute += params.offer_window_min
    open_minute = minute                                               # everyone has had a turn, so now it's open to all
    # Anyone who said yes too late can now claim it, fastest first.
    for late_reply_minute, recipient in sorted(late, key=lambda x: x[0]):
        claim_minute = max(late_reply_minute, open_minute) + confirm
        if claim_minute >= donation.deadline:
            break
        kind, minute_after = _attempt(params, flags, recipient, donation, claim_minute, rng)
        if kind == "delivered":
            return _deliver(recipient, donation, claim_minute), wasted
        wasted += kind == "wasted"
        open_minute = _after_failure(params, flags, kind, claim_minute, minute_after)
    return None, wasted


def run_once(params: Params, flags: Flags, seed: int, salt: int = 0) -> dict:
    # Build the world for this seed (every scenario sees the same organisations and listings).
    recipients, donations = make_env(params, seed)
    # A separate random stream for "what happens" (salt makes different scenarios roll different dice).
    rng = random.Random(seed * 7919 + salt)
    # Totals: units listed, units delivered, wasted trips.
    listed = delivered = wasted = 0
    # times = claim times of delivered listings; day = the day we're currently on.
    times, day = [], -1
    for donation in donations:
        if donation.day != day:
            # A new day: everyone's storage space resets.
            day = donation.day
            for recipient in recipients:
                recipient.cap_left = recipient.cap
        # Count the listed units.
        listed += donation.qty
        # Run this listing through the process being simulated.
        minute, wasted_trips = _handle(params, flags, recipients, donation, rng)
        wasted += wasted_trips
        if minute is not None:
            # It was delivered: count the units and remember how long it took.
            delivered += donation.qty
            times.append(minute)
    # Units each recipient ended up with.
    units = [recipient.units for recipient in recipients]
    return {"donations": len(donations), "listed": listed, "delivered": delivered, "wasted": wasted, "times": times,
            "completion": delivered / listed if listed else None,
            "median_time": statistics.median(times) if times else None,
            "wasted_per_100": 100 * wasted / len(donations) if donations else None,
            "top_share": max(units) / sum(units) if sum(units) else None}


def run_many(params: Params, flags: Flags, runs: int, seed: int = 1, salt: int = 0) -> list:
    # Repeat the simulation many times, each with a different seed, so we get a spread of outcomes.
    return [run_once(params, flags, seed + i, salt) for i in range(runs)]


def _q(values, fraction):
    # A percentile: e.g. fraction 0.5 is the median and 0.95 is the 95th percentile. Missing values are ignored.
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    # The exact (possibly between-two-values) position we want...
    position = (len(values) - 1) * fraction
    # ...so we blend the two values on either side of it.
    low, high = math.floor(position), math.ceil(position)
    return values[low] + (values[high] - values[low]) * (position - low)


def summarise(runs: list) -> dict:
    out = {}
    # For each result we care about, give the median plus the 5th and 95th percentiles (the "typical range").
    for metric in ("completion", "median_time", "wasted_per_100", "top_share"):
        values = [recipient[metric] for recipient in runs]
        out[metric] = {"median": _q(values, .5), "p5": _q(values, .05), "p95": _q(values, .95)}
    return out


def simulate(params: Params, runs: int = 100, seed: int = 1, scenarios=None) -> dict:
    # Which scenarios to run (all of them unless told otherwise).
    names = list(scenarios or SCENARIOS)
    # Run every scenario many times; "index" is the salt so each scenario rolls different dice.
    results = {scenario: run_many(params, SCENARIOS[scenario], runs, seed, salt=index) for index, scenario in enumerate(names)}
    # The packaged answer, with a disclaimer that this is only a model.
    out = {"disclaimer": "SIMULATION under stated assumptions. Not a real-world result.", "runs": runs, "seed": seed,
           "assumptions": asdict(params), "scenarios": {scenario: {"label": LABELS[scenario], **summarise(results[scenario])} for scenario in names}}
    if "baseline" in results and "full" in results:
        # Compare FOODBRIDGE and today week by week (on the same week, same world).
        pairs = [(factors["completion"], baseline_run["completion"]) for factors, baseline_run in zip(results["full"], results["baseline"]) if factors["completion"] is not None]
        # Count the weeks where FOODBRIDGE did better, tied, or did worse (the tiny 1e-9 avoids rounding noise).
        out["paired"] = {"full_better": sum(factors > baseline_run + 1e-9 for factors, baseline_run in pairs), "tie": sum(abs(factors - baseline_run) <= 1e-9 for factors, baseline_run in pairs),
                         "full_worse": sum(factors < baseline_run - 1e-9 for factors, baseline_run in pairs), "runs": len(pairs)}
    return out
