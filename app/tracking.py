"""Read-only views: looking, never touching.

Nothing in here changes any data. Every number comes from the saved event log, nothing is guessed or made up.
The route planner just tries every order for a handful of stops (and tells you its assumptions with every plan).
No machine learning, no magic.
"""
import itertools  # gives us every possible ordering of stops (permutations)
import json  # to read event details back out of the log
from collections import Counter  # a dictionary that counts things
from datetime import datetime, timedelta  # date+time and lengths of time
from sqlalchemy import or_, select  # "this OR that" in queries, and select for queries
from sqlalchemy.orm import Session  # the type of a database conversation
from . import matching, actions  # ranking rules and the main app actions
from .proof_chain import verify_chain  # checks the tamper-evident log
from .database import utcnow  # our UTC clock
from .tables import AuditEvent, Claim, Donation, Incident, Organisation  # the tables we read

# The five steps every rescue goes through, in order.
STAGES = ["listed", "offered", "claimed", "pickup", "handover"]
HANDLING_MIN = 5                 # minutes we assume you spend at each stop (we say this out loud in every plan)
MAX_PLAN_STOPS = 7               # max stops we plan for: we try EVERY order, and 7! = 5040 is still fast
# A listing is "at risk" when fewer than 20 minutes of safe time remain.
AT_RISK_WINDOW_MIN = 20
# A claim is "about to lapse" when fewer than 5 minutes remain.
CLAIM_RISK_MIN = 5
# The only event details we show on a passport (so private notes can't leak out by accident).
SAFE_DETAIL_KEYS = ("quantity", "accepted_quantity", "reason", "declared", "donation_status", "froze", "decision")


def _iso_z(dt):
    # Turn a date+time into UTC text ending in Z, or None if there isn't one.
    return dt.isoformat() + "Z" if dt else None


def donation_events(db: Session, donation_id: int) -> list:
    # Event details are saved as JSON text, so we search the text for the donation id. Two patterns: id followed by a comma, or id at the end of the braces.
    patterns = [f'%"donation_id":{donation_id},%', f'%"donation_id":{donation_id}}}%']
    # Fetch every event matching either pattern, oldest first.
    rows = db.scalars(select(AuditEvent).where(or_(*[AuditEvent.payload.like(payload) for payload in patterns])).order_by(AuditEvent.id)).all()
    out = []
    for event in rows:
        # Turn the saved JSON text back into a dictionary.
        payload = json.loads(event.payload)
        if payload.get("donation_id") == donation_id:        # double-check the exact id, because the LIKE search alone could mix up ids (like 1 vs 10)
            out.append((event, payload))
    return out


# ---------------- live tracking (where's the food rn?) ----------------
def timeline(db: Session, listing: Donation, now: datetime | None = None) -> dict:
    # Use the real time unless a test passes a fake one.
    now = now or utcnow()
    # Every logged event for this listing.
    events = donation_events(db, listing.id)
    # When each kind of event FIRST happened, and when it LAST happened.
    first = {}
    last = {}
    for event, payload in events:
        first.setdefault(event.type, event.ts)
        last[event.type] = event.ts
    # The newest claim on this listing, if any.
    claim = db.scalars(select(Claim).where(Claim.donation_id == listing.id).order_by(Claim.id.desc())).first()
    # Only count it as "live" if it's active, frozen or finished (not expired etc.).
    live_claim = claim if claim and claim.status in ("reserved", "frozen", "completed") else None
    # How many offers were made, and how many claims lapsed.
    offers = sum(1 for event, _ in events if event.type == "OFFER_MADE")
    expired_claims = sum(1 for event, _ in events if event.type == "CLAIM_EXPIRED")

    # When each stage was reached (None = not reached yet).
    done = {
        "listed": first.get("DONATION_PUBLISHED"),
        "offered": first.get("OFFER_MADE"),
        "claimed": last.get("CLAIM_RESERVED") if live_claim else None,
        "pickup": last.get("PICKUP_VERIFIED") if live_claim and live_claim.picked_up_at else None,
        "handover": last.get("HANDOVER_COMPLETED") if listing.status == "collected" else None,
    }
    # current stage = the first stage that isn't done yet (the offer stage gets skipped if the listing was open to everyone)
    states = {}
    current = None
    for stage in STAGES:
        if done[stage]:
            # Finished stage.
            states[stage] = "done"
        elif stage == "offered" and (done["claimed"] or not offers):
            # No exclusive offer happened (or it's already claimed), so this stage doesn't apply.
            states[stage] = "skipped"
        else:
            if current is None and listing.status in ("published", "reserved", "frozen"):
                # The first unfinished stage of a live listing is the "current" one.
                current = stage
            states[stage] = "current" if current == stage else "pending"

    # Start by assuming the rescue is still going.
    outcome = {"result": "in_progress", "reason_code": None, "detail": None}
    if listing.status == "collected":
        # Success: the handover was confirmed.
        outcome = {"result": "success", "reason_code": "HANDOVER_CONFIRMED", "detail": None}
    elif listing.status == "expired":
        # Failure: nobody took it before the window closed.
        outcome = {"result": "failed", "reason_code": "NO_TAKER_BEFORE_DEADLINE",
                   "detail": f"{offers} offer(s) made, {expired_claims} claim(s) lapsed before the safe window closed"}
    elif listing.status == "rejected":
        # Failure: refused at the door. Find the most recent recorded reason.
        reason = next((payload.get("reason") for event, payload in reversed(events) if event.type == "HANDOVER_REFUSED"), None)
        outcome = {"result": "failed", "reason_code": "REFUSED_AT_HANDOVER", "detail": reason}
    elif listing.status == "discarded":
        # Failure: thrown away after a problem report.
        outcome = {"result": "failed", "reason_code": "DISCARDED_AFTER_INCIDENT", "detail": None}
    elif listing.status == "frozen":
        # Paused while an admin reviews a problem report.
        outcome = {"result": "on_hold", "reason_code": "INCIDENT_REVIEW", "detail": None}
    if outcome["result"] == "failed":
        # Mark the first not-yet-reached stage as "failed" so the progress bar shows where it went wrong.
        next_pending = next((stage for stage in STAGES if states[stage] == "pending"), None)
        if next_pending:
            states[next_pending] = "failed"

    # Warning flags for things that need attention.
    risks = []
    if listing.status in ("published", "reserved") and listing.latest_collect > now:
        if listing.latest_collect - now <= timedelta(minutes=AT_RISK_WINDOW_MIN):
            # The safe window is about to close.
            risks.append("WINDOW_CLOSING")
    if listing.status == "reserved" and live_claim and live_claim.status == "reserved" and not live_claim.picked_up_at:
        if live_claim.expires_at - now <= timedelta(minutes=CLAIM_RISK_MIN):
            # The claim is about to lapse and nobody has picked up yet.
            risks.append("CLAIM_EXPIRING")
    if listing.status == "published" and not actions.active_offer(listing, now) and offers and not first.get("CLAIM_RESERVED"):
        # Offers were made but nobody took it, and nobody has first dibs right now.
        risks.append("NO_TAKER_YET")
    return {
        "donation_id": listing.id, "status": listing.status, "stages": [{"key": stage, "state": states[stage], "at": _iso_z_from_text(done[stage])} for stage in STAGES],
        "outcome": outcome, "risks": risks,
        "latest_collect": _iso_z(listing.latest_collect),
        "claim_expires_at": _iso_z(live_claim.expires_at) if live_claim and live_claim.status == "reserved" else None,
        "offers_made": offers, "claims_lapsed": expired_claims,
    }


def _iso_z_from_text(ts):
    # Event timestamps are saved as text; just add the Z (UTC marker) if it's missing.
    return ts + "Z" if ts and not ts.endswith("Z") else ts


# ---------------- food passport (the full receipts) ----------------
def passport(db: Session, listing: Donation, base_url: str, now: datetime | None = None) -> dict:
    # Imported here (not at the top) so the QR library is only loaded when a passport is actually made.
    from . import qr
    now = now or utcnow()
    # Who posted the food.
    donor = db.get(Organisation, listing.donor_id)
    # The full handling history, one entry per logged event.
    history = []
    for event, payload in donation_events(db, listing.id):
        # Only the safe detail keys are shown, and only the first 12 characters of the fingerprint.
        history.append({"at": _iso_z_from_text(event.ts), "type": event.type, "hash": event.hash[:12],
                        "detail": {key: payload[key] for key in SAFE_DETAIL_KEYS if key in payload}})
    # Every problem report filed against this listing.
    incidents = [{"id": incident.id, "note": incident.note, "status": incident.status, "resolution": incident.resolution,
                  "created_at": _iso_z(incident.created_at), "resolved_at": _iso_z(incident.resolved_at)}
                 for incident in db.scalars(select(Incident).where(Incident.donation_id == listing.id).order_by(Incident.id))]
    # Just the refusals, pulled out of the history.
    refusals = [entry for entry in history if entry["type"] == "HANDOVER_REFUSED"]
    # The link the passport's QR code points to.
    url = f"{base_url.rstrip('/')}/#passport/{listing.id}"
    # Check the whole log is intact, to include in the passport.
    chain = verify_chain(db)
    return {
        "donation_id": listing.id, "passport_ref": f"FB-P{listing.id}",
        "food": {"category": listing.category, "storage": listing.storage, "quantity": listing.quantity,
                 "prepared_at": _iso_z(listing.prepared_at), "donor_deadline": _iso_z(listing.donor_deadline),
                 "latest_collect": _iso_z(listing.latest_collect)},
        "donor": donor.name, "donor_declaration": listing.declaration or None, "status": listing.status,
        "timeline": timeline(db, listing, now), "handling_history": history, "incidents": incidents, "refusals": refusals,
        "record_integrity": {"chain_ok": chain["ok"], "events_checked": chain["checked"]},
        "url": url, "qr_svg": qr.qr_svg(url),
        "disclaimer": "This passport records what was declared and what happened. It does not certify that the food is safe; "
                      "eligibility rules need review by a qualified food-safety professional.",
    }


# ---------------- real impact (no fake numbers) ----------------
def _minutes_between(a: str, b: str) -> float:
    # Minutes from timestamp a to timestamp b (both saved as text).
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60


def impact_detail(db: Session) -> dict:
    """Everything is recalculated from saved events and rows. No made-up meal or CO2 numbers, promise."""
    # The headline totals first.
    base = actions.impact(db)
    # When each listing was published, first claimed and completed (keyed by listing id).
    published, first_claim, completed_at = {}, {}, {}
    # Which food type each listing was, and the per-food-type totals.
    category_of, by_category = {}, {}
    # How often each refusal reason was given.
    refusal_reasons = Counter()
    # How many claims lapsed.
    claims_lapsed = 0
    # Walk through the whole log in order.
    for event in db.scalars(select(AuditEvent).order_by(AuditEvent.id)):
        payload = json.loads(event.payload)
        donation_id = payload.get("donation_id")
        if event.type == "DONATION_PUBLISHED":
            # Remember when it was published and what type of food it is.
            published[donation_id] = event.ts
            category_of[donation_id] = payload["category"]
            # Start (or find) this food type's tally and add the listed units.
            bucket = by_category.setdefault(payload["category"], {"listed": 0, "collected": 0, "uncollected": 0})
            bucket["listed"] += payload["quantity"]
        elif event.type == "CLAIM_RESERVED":
            # Keep only the FIRST claim time for each listing.
            first_claim.setdefault(donation_id, event.ts)
        elif event.type == "HANDOVER_COMPLETED":
            # Remember when it finished and add the accepted units to that food type's "collected".
            completed_at[donation_id] = event.ts
            by_category.setdefault(category_of.get(donation_id, "unknown"), {"listed": 0, "collected": 0, "uncollected": 0})["collected"] += payload["accepted_quantity"]
        elif event.type == "HANDOVER_REFUSED":
            # Count the refusal reason (tidied up: trimmed, lowercase, max 60 characters, or "no reason given").
            refusal_reasons[(payload.get("reason") or "").strip().lower()[:60] or "no reason given"] += 1
        elif event.type in ("DONATION_EXPIRED", "DONATION_DISCARDED"):
            # Add the wasted units to that food type's "uncollected".
            by_category.setdefault(category_of.get(donation_id, "unknown"), {"listed": 0, "collected": 0, "uncollected": 0})["uncollected"] += payload["quantity"]
        elif event.type == "CLAIM_EXPIRED":
            claims_lapsed += 1
    # How many listings are in each status right now.
    statuses = Counter(listing.status for listing in db.scalars(select(Donation)))
    # "Finished" = any listing that has reached an end state.
    finished = statuses["collected"] + statuses["expired"] + statuses["rejected"] + statuses["discarded"]
    # Minutes from listing to collection, and from listing to first claim (only for listings we have a publish time for).
    minutes_to_collect = [_minutes_between(published[donation_key], timestamp) for donation_key, timestamp in completed_at.items() if donation_key in published]
    minutes_to_first_claim = [_minutes_between(published[donation_key], timestamp) for donation_key, timestamp in first_claim.items() if donation_key in published]
    # Why food wasn't collected: how many listings ended in each failure way.
    reasons = Counter()
    reasons["NO_TAKER_BEFORE_DEADLINE"] = statuses["expired"]
    reasons["REFUSED_AT_HANDOVER"] = statuses["rejected"]
    reasons["DISCARDED_AFTER_INCIDENT"] = statuses["discarded"]
    # Every organisation except the admin.
    orgs = db.scalars(select(Organisation).where(Organisation.role != "admin")).all()
    # Small helper: the average of a list, rounded to 1 decimal (None if the list is empty).
    average = lambda xs: round(sum(xs) / len(xs), 1) if xs else None
    return {
        **base,
        "donations_by_status": dict(statuses),
        "completion_rate_pct": round(100 * statuses["collected"] / finished) if finished else None,
        "finished_donations": finished,
        "avg_minutes_listing_to_collection": average(minutes_to_collect),
        "avg_minutes_listing_to_first_claim": average(minutes_to_first_claim),
        "claims_lapsed": claims_lapsed,
        "failure_reasons": {donation_key: timestamp for donation_key, timestamp in reasons.items() if timestamp},
        "refusal_reasons": dict(refusal_reasons.most_common(5)),
        "by_category": by_category,
        "participants": {
            "donors": sum(1 for o in orgs if o.role == "donor" and o.verified),
            "recipients": sum(1 for o in orgs if o.role == "recipient" and o.verified),
            "recipients_with_completed_handover": sum(1 for o in orgs if o.role == "recipient" and o.completed > 0),
        },
        "method_note": "Units are exactly what donors listed (packs or items). No meal or emissions estimates are made, "
                       "because unit sizes are not recorded; any such estimate would need disclosed assumptions and uncertainty.",
    }


# ---------------- rescue route planner (who do I pick up from first?) ----------------
def _simulate(points, start, order, capacity, speed):
    """Drive through the stops in the given order, starting at minute 0. If a stop can't be done
    (too late or the vehicle is full), we just skip it."""
    # clock_min = minutes since we set off, position = where we are, load = units picked up so far, served = stops we managed, stop_info = details per stop.
    clock_min, position, load, served, stop_info = 0.0, start, 0, [], {}
    for stop_index in order:
        point = points[stop_index]
        # Travel time from where we are to this stop.
        travel = matching.haversine_km(position[0], position[1], point["lat"], point["lon"]) / speed * 60
        # The minute we'd arrive.
        arrive = clock_min + travel
        # Skip this stop if we'd be late or the vehicle can't fit the food.
        if arrive > point["deadline_min"] or load + point["quantity"] > capacity:
            continue
        # We can do it: load up the food and record the stop.
        load += point["quantity"]
        served.append(stop_index)
        stop_info[stop_index] = {"leg_min": travel, "arrive_min": arrive, "load": load}
        # Time moves on by the handling time, and we're now at this stop's location.
        clock_min = arrive + HANDLING_MIN
        position = (point["lat"], point["lon"])
    return served, stop_info, clock_min


def _best(points, start, capacity, speed, exclude=()):
    # Every stop number we're allowed to use (minus any excluded).
    stop_indexes = [stop_index for stop_index in range(len(points)) if stop_index not in exclude]
    best = None
    # Try EVERY possible order of those stops.
    for order_tried in itertools.permutations(stop_indexes):
        served, at, total_min = _simulate(points, start, order_tried, capacity, speed)
        # Total units rescued by this order.
        units = sum(points[stop_index]["quantity"] for stop_index in served)
        # How many of those units only this recipient can rescue (a tie-breaker).
        risk_units = sum(points[stop_index]["quantity"] for stop_index in served if points[stop_index]["only_you"])
        # Better = more units, then more "only you" units, then a shorter trip.
        score_key = (units, risk_units, -total_min)
        if best is None or score_key > best[0]:
            best = (score_key, served, at, total_min)
    return best


def plan_route(db: Session, recipient: Organisation, now: datetime | None = None, vehicle_capacity: int | None = None) -> dict:
    now = now or utcnow()
    speed = matching.SPEED_KMPH
    # Vehicle capacity defaults to the recipient's spare storage capacity (and never below zero).
    capacity = recipient.capacity_remaining if vehicle_capacity is None else max(0, vehicle_capacity)
    # Start from the recipient's own location.
    start = (recipient.lat, recipient.lon)
    # Every stop we could include.
    candidates = []

    # First: claims this recipient has already made but not yet picked up.
    for candidate in db.scalars(select(Claim).where(Claim.recipient_id == recipient.id, Claim.status == "reserved", Claim.picked_up_at.is_(None))):
        listing = db.get(Donation, candidate.donation_id)
        donor = db.get(Organisation, listing.donor_id)
        # The deadline is whichever comes first: the claim lapsing or the food's window closing.
        deadline = min(candidate.expires_at, listing.latest_collect)
        candidates.append(dict(listing=listing, donor=donor, claim_id=candidate.id, deadline=deadline))
    # Second: open listings this recipient could claim, soonest deadline first.
    for listing in db.scalars(select(Donation).where(Donation.status == "published").order_by(Donation.latest_collect)):
        # Skip closed listings and ones currently offered exclusively to somebody else.
        if listing.latest_collect <= now or (actions.active_offer(listing, now) and listing.offered_to != recipient.id):
            continue
        donor = db.get(Organisation, listing.donor_id)
        # Skip listings this recipient isn't eligible for.
        if not matching.evaluate(listing, donor, recipient, now).eligible:
            continue
        candidates.append(dict(listing=listing, donor=donor, claim_id=None, deadline=listing.latest_collect))

    # Most urgent first, then keep only the first 7 (the rest are "overflow" and get mentioned as skipped).
    candidates.sort(key=lambda candidate: candidate["deadline"])
    overflow = candidates[MAX_PLAN_STOPS:]
    candidates = candidates[:MAX_PLAN_STOPS]
    # Turn each candidate into a "point" with the numbers the route search needs.
    points = []
    for candidate in candidates:
        listing = candidate["listing"]
        # Everyone who could take this listing; if that's only this recipient, mark the stop "only_you".
        eligible_ids = {r.org_id for r in actions.matches_for(db, listing.id, now)["ranked"]}
        points.append(dict(candidate, donation_id=listing.id, lat=candidate["donor"].lat, lon=candidate["donor"].lon, quantity=listing.quantity,
                           deadline_min=(candidate["deadline"] - now).total_seconds() / 60,
                           only_you=eligible_ids <= {recipient.id}))

    # The plan we'll fill in and return (starts empty, with the assumptions written out).
    plan = {"vehicle_capacity": capacity, "assumptions": {"speed_kmph": speed, "handling_min_per_stop": HANDLING_MIN,
                                                     "distance": "straight-line (real roads will be longer)",
                                                     "claim_note": "Claim only the next stop: a claim lapses 15 minutes after it is made."},
            "stops": [], "skipped": [], "total_units": 0, "total_minutes": 0, "next_stop": None, "backup": None}
    # Nothing to plan, or no room in the vehicle: return the empty plan.
    if not points or capacity <= 0:
        return plan
    # Find the best order of stops.
    best_key, served, stop_info, total_min_needed = _best(points, start, capacity, speed)

    def stop_json(stop_index):
        # Package one stop's details for the website.
        point, leg = points[stop_index], stop_info[stop_index]
        return {"donation_id": point["donation_id"], "donor": point["donor"].name, "category": point["listing"].category,
                "storage": point["listing"].storage, "quantity": point["quantity"], "claimed": point["claim_id"] is not None,
                "claim_id": point["claim_id"], "leg_min": round(leg["leg_min"]), "arrive_in_min": round(leg["arrive_min"]),
                "slack_min": round(point["deadline_min"] - leg["arrive_min"]), "cumulative_units": leg["load"],
                "urgent": point["deadline_min"] <= 30, "only_you": point["only_you"], "deadline": _iso_z(point["deadline"])}
    # Fill in the plan's stops, totals and the very next stop.
    plan["stops"] = [stop_json(stop_index) for stop_index in served]
    plan["total_units"] = best_key[0]
    plan["total_minutes"] = round(total_min_needed)
    plan["next_stop"] = plan["stops"][0] if plan["stops"] else None

    # Explain why each left-out stop was left out.
    for stop_index, point in enumerate(points):
        if stop_index in served:
            continue
        # How long the direct trip from the start would take.
        direct = matching.haversine_km(start[0], start[1], point["lat"], point["lon"]) / speed * 60
        if direct > point["deadline_min"]:
            # Even going straight there you'd be too late.
            why = "WOULD_MISS_DEADLINE"
        elif point["quantity"] > capacity:
            # It simply doesn't fit in the vehicle.
            why = "EXCEEDS_VEHICLE_CAPACITY"
        else:
            # It's doable on its own, but the best overall route uses the time or space elsewhere.
            why = "BETTER_ROUTE_USES_THE_TIME_OR_SPACE"
        plan["skipped"].append({"donation_id": point["donation_id"], "donor": point["donor"].name, "category": point["listing"].category,
                                "quantity": point["quantity"], "reason": why})
    # Stops beyond the 7-stop limit were never considered.
    for candidate in overflow:
        plan["skipped"].append({"donation_id": candidate["listing"].id, "donor": candidate["donor"].name, "category": candidate["listing"].category,
                                "quantity": candidate["listing"].quantity, "reason": "NOT_CONSIDERED_TOO_MANY_OPTIONS"})

    if served:                                   # plan B in case someone else grabs the first stop or it falls through
        # Re-plan without the first stop.
        backup_plan = _best(points, start, capacity, speed, exclude={served[0]})
        if backup_plan and backup_plan[1]:
            plan["backup"] = {"without_donation_id": points[served[0]]["donation_id"], "total_units": backup_plan[0][0],
                              "stops": [{"donation_id": points[stop_index]["donation_id"], "donor": points[stop_index]["donor"].name,
                                         "quantity": points[stop_index]["quantity"], "arrive_in_min": round(backup_plan[2][stop_index]["arrive_min"])} for stop_index in backup_plan[1]]}
    return plan
