# The main things the app can DO: create organisations, publish food, claim it, hand it over, expire it, report problems.
import hmac  # for safe comparisons and signing
import json  # to read event details back out of the log
import os  # to read settings
import re  # pattern matching for pickup codes
from collections import Counter  # a dictionary that counts things
from datetime import datetime, timedelta  # date+time and lengths of time
from hashlib import sha256  # the fingerprint function used for pickup codes
from sqlalchemy import func, select  # func.sum etc., and select for queries
from sqlalchemy.exc import IntegrityError  # the error the database throws when a rule (like "one claim per listing") is broken
from sqlalchemy.orm import Session  # the type of a database conversation
from . import security, matching, safe_window  # login secret, ranking rules, and food-safety timing
from .proof_chain import append_event  # writes to the tamper-evident log
from .database import utcnow  # our UTC clock
from .tables import AuditEvent, Claim, Donation, Incident, Organisation  # the tables we work with

# A claim lapses after 15 minutes if the pickup isn't confirmed.
CLAIM_TIMEOUT_MIN = 15
# A pickup code still works for 15 minutes after the food's safe window closes.
PICKUP_GRACE_MIN = 15


# Three kinds of problems the app can raise; main.py turns them into 404 / 409 / 403 replies.
class NotFound(Exception): ...  # the thing you asked for doesn't exist
class Conflict(Exception): ...  # the request clashes with the current state (already claimed, wrong status, ...)
class Forbidden(Exception): ...  # you're not allowed to do that


def offer_window_min() -> int:
    """How many minutes the best-ranked recipient gets first dibs. Set to 0 to turn off exclusive offers."""
    try:
        # Read the setting (default "2"), turn it into a whole number, and never go below 0.
        return max(0, int(os.getenv("FOODBRIDGE_OFFER_WINDOW_MIN", "2")))
    except ValueError:
        # If someone typed something that isn't a number, fall back to 2.
        return 2


def _already_offered_ids(listing: Donation) -> set:
    # Turn the saved "1,4,7" text back into a set of numbers (the "if row" skips empty pieces).
    return {int(row) for row in listing.tried_ids.split(",") if row}


def _remember_offered(listing: Donation, org_id: int):
    # Add this organisation to the "already had a turn" list and save it back as sorted comma-separated text.
    listing.tried_ids = ",".join(str(x) for x in sorted(_already_offered_ids(listing) | {org_id}))


# ---------------- organisations (the people and places) ----------------
def create_org(db: Session, **fields) -> Organisation:
    # Build the organisation from whatever details were passed in.
    org = Organisation(**fields)
    # Add it and flush so it gets its id number (we need the id for the log entry).
    db.add(org)
    db.flush()
    # Write "an organisation was created" to the tamper-evident log.
    append_event(db, "ORG_CREATED", {"org_id": org.id, "role": org.role})
    # Save everything for real.
    db.commit()
    return org


def verify_org(db: Session, org_id: int):
    # Look the organisation up by id.
    org = db.get(Organisation, org_id)
    # If it doesn't exist, say so.
    if not org:
        raise NotFound("organisation")
    # Mark them approved.
    org.verified = True
    # Log the approval, then save.
    append_event(db, "ORG_VERIFIED", {"org_id": org_id})
    db.commit()


# ---------------- listings, matching, offers (posting food and finding who gets it) ----------------
def matches_for(db: Session, donation_id: int, now: datetime | None = None) -> dict:
    # Use the real time unless a test passes a fake one.
    now = now or utcnow()
    # Find the listing; stop if it doesn't exist.
    listing = db.get(Donation, donation_id)
    if not listing:
        raise NotFound("donation")
    # Who posted it?
    donor = db.get(Organisation, listing.donor_id)
    # Every recipient organisation (variable name "recs" = recipients).
    recs = db.scalars(select(Organisation).where(Organisation.role == "recipient")).all()
    # Check each recipient against this listing using the matching rules.
    results = [matching.evaluate(listing, donor, r, now) for r in recs]
    # Give back the ranked eligible ones, and the excluded ones (with their reasons).
    return {"ranked": matching.rank(results), "excluded": [row for row in results if not row.eligible]}


def advance_offer(db: Session, listing: Donation, now: datetime, window: int):
    """Give first dibs to the next best recipient who hasn't had a turn yet; when nobody's left, open the listing to everyone."""
    # A window of 0 means "no exclusive offers": clear any offer and leave the listing open to everyone.
    if window <= 0:
        listing.offered_to = listing.offer_expires_at = None
        return
    # Find the best-ranked recipient who hasn't had a turn yet (or None if everyone has).
    next_pick = next((r for r in matches_for(db, listing.id, now)["ranked"] if r.org_id not in _already_offered_ids(listing)), None)
    if next_pick:
        # Give them first dibs...
        listing.offered_to = next_pick.org_id
        # ...until the window ends (but never past the food's safe deadline).
        listing.offer_expires_at = min(now + timedelta(minutes=window), listing.latest_collect)
        # Remember they've had a turn so they don't get it again.
        _remember_offered(listing, next_pick.org_id)
        # Log the offer.
        append_event(db, "OFFER_MADE", {"donation_id": listing.id, "recipient_id": next_pick.org_id, "expires_at": listing.offer_expires_at.isoformat()})
    elif listing.offered_to is not None:
        # Nobody's left and there was an active offer: open the listing to everyone and log it.
        listing.offered_to = listing.offer_expires_at = None
        append_event(db, "OFFER_OPEN_TO_ALL", {"donation_id": listing.id})


def publish_donation(db: Session, rules: dict, donor_id: int, category: str, storage: str,
                     quantity: int, prepared_at: datetime, donor_deadline: datetime,
                     now: datetime | None = None, offer_window: int | None = None,
                     declaration: str = "") -> Donation:
    # Use the real time unless a test passes a fake one.
    now = now or utcnow()
    # Look up the donor.
    donor = db.get(Organisation, donor_id)
    # Only approved donors can post food.
    if not donor or donor.role != "donor" or not donor.verified:
        raise Conflict("donor must be a verified donor")
    # Zero or negative quantities make no sense.
    if quantity <= 0:
        raise Conflict("quantity must be positive")
    try:
        # Work out the real deadline using the safe-window rules.
        latest = safe_window.latest_collect(rules, category, storage, prepared_at, donor_deadline)
    except safe_window.RuleError as e:
        # No rule for this food type / storage combo, so we refuse.
        raise Conflict(str(e))
    # If the safe window has already closed, there's nothing to publish.
    if latest <= now:
        raise Conflict("safe window already closed")
    # Build the listing (the declaration is trimmed and cut to 300 characters).
    listing = Donation(donor_id=donor_id, category=category, storage=storage, quantity=quantity,
                 prepared_at=prepared_at, donor_deadline=donor_deadline, latest_collect=latest,
                 declaration=(declaration or "").strip()[:300])
    # Add it and flush so it gets an id.
    db.add(listing)
    db.flush()
    # Log that it was published.
    append_event(db, "DONATION_PUBLISHED", {"donation_id": listing.id, "donor_id": donor_id, "category": category,
                                           "quantity": quantity, "latest_collect": latest.isoformat(),
                                           "declared": bool(listing.declaration), "declaration": listing.declaration})
    # Give first dibs to the best recipient (using the default window unless one was passed in).
    advance_offer(db, listing, now, offer_window_min() if offer_window is None else offer_window)
    # Save everything.
    db.commit()
    return listing


def active_offer(listing: Donation, now: datetime) -> bool:
    # True only if someone currently has first dibs AND their turn hasn't run out yet.
    return bool(listing.offered_to and listing.offer_expires_at and now < listing.offer_expires_at)


# ---------------- claims (calling dibs) ----------------
def claim(db: Session, donation_id: int, recipient_id: int, now: datetime | None = None) -> Claim:
    # Use the real time unless a test passes a fake one.
    now = now or utcnow()
    # Look up both sides.
    listing = db.get(Donation, donation_id)
    recipient = db.get(Organisation, recipient_id)
    if not listing or not recipient:
        raise NotFound("donation or recipient")
    # Can only claim a listing that is still published and not past its deadline.
    if listing.status != "published" or now >= listing.latest_collect:
        raise Conflict("listing is not available")
    # If someone else currently has first dibs, you have to wait.
    if active_offer(listing, now) and listing.offered_to != recipient.id:
        raise Conflict("offered to another organisation first; it opens to everyone shortly")
    # If this recipient already let a claim on this listing expire, they can't try again (no-shows stay blocked).
    if db.scalar(select(Claim.id).where(Claim.donation_id == listing.id, Claim.recipient_id == recipient.id, Claim.status == "expired")):
        raise Conflict("your earlier claim on this listing expired")
    # Re-check all the matching rules right now (the situation may have changed since the list was shown).
    match_result = matching.evaluate(listing, db.get(Organisation, listing.donor_id), recipient, now)
    if not match_result.eligible:
        raise Conflict("not eligible: " + ",".join(match_result.excluded_reasons))
    # Create the claim; it lapses in 15 minutes or at the food's deadline, whichever is sooner.
    claim = Claim(donation_id=listing.id, recipient_id=recipient.id,
              expires_at=min(now + timedelta(minutes=CLAIM_TIMEOUT_MIN), listing.latest_collect))
    db.add(claim)
    try:
        db.flush()                      # the unique index in the database picks the winner if two people claim at once
    except IntegrityError:
        # Someone else got there first: undo and tell the loser.
        db.rollback()
        raise Conflict("already reserved")
    # The listing is now reserved, and any first-dibs offer is cleared.
    listing.status = "reserved"
    listing.offered_to = listing.offer_expires_at = None
    # Count this as an attempt for the recipient's reliability score.
    recipient.attempts += 1
    # Log the claim, then save.
    append_event(db, "CLAIM_RESERVED", {"claim_id": claim.id, "donation_id": listing.id, "recipient_id": recipient.id})
    db.commit()
    return claim


def pickup_code(claim_id: int) -> str:
    """Made from the server secret, so there's nothing to store, nobody can guess it, and the claimant can see it again anytime."""
    # Code looks like FB-<claim id>-<first 12 characters of a secret-based signature>.
    return f"FB-{claim_id}-" + hmac.new(security.SECRET.encode(), f"pickup|{claim_id}".encode(), sha256).hexdigest()[:12]


def verify_pickup(db: Session, donor_id: int, code: str, now: datetime | None = None) -> dict:
    # Use the real time unless a test passes a fake one.
    now = now or utcnow()
    # One shared "invalid code" error, so we never reveal WHY a code was wrong.
    bad_code_error = Conflict("invalid code")
    # The code must look like fb-<digits>-<12 hex characters> (we lowercase it first so upper/lower case both work).
    code_match = re.fullmatch(r"fb-(\d+)-([0-9a-f]{12})", (code or "").strip().lower())
    if not code_match:
        raise bad_code_error
    # Find the claim the code points to.
    claim = db.get(Claim, int(code_match[1]))
    # The code must be exactly the one we'd generate for that claim (compared safely).
    if not claim or not hmac.compare_digest(pickup_code(claim.id).lower(), code_match[0]):
        raise bad_code_error
    # Look up the listing the claim belongs to.
    listing = db.get(Donation, claim.donation_id)
    # Only the donor who posted the food can use the code.
    if listing.donor_id != donor_id:
        raise bad_code_error                     # never leak other donors' claims
    # Each code works once.
    if claim.picked_up_at:
        raise Conflict("this code was already used")
    # The claim must still be active.
    if claim.status != "reserved":
        raise Conflict("this claim is no longer active")
    # Too late: the safe window (plus the grace period) is over.
    if now > listing.latest_collect + timedelta(minutes=PICKUP_GRACE_MIN):
        raise Conflict("this code has expired")
    # All good: record the moment the donor confirmed the pickup.
    claim.picked_up_at = now
    append_event(db, "PICKUP_VERIFIED", {"claim_id": claim.id, "donation_id": listing.id})
    db.commit()
    # Tell the donor who's collecting what.
    recipient = db.get(Organisation, claim.recipient_id)
    return {"claim_id": claim.id, "recipient": recipient.name, "category": listing.category, "quantity": listing.quantity}


def complete_handover(db: Session, claim_id: int, accepted: bool, accepted_quantity: int = 0,
                      reason: str | None = None, now: datetime | None = None) -> Claim:
    # Use the real time unless a test passes a fake one.
    now = now or utcnow()
    # Find the claim.
    claim = db.get(Claim, claim_id)
    if not claim:
        raise NotFound("claim")
    # Only an active claim can be handed over.
    if claim.status != "reserved":
        raise Conflict("claim is not active")
    # Look up the listing and the recipient involved.
    listing = db.get(Donation, claim.donation_id)
    recipient = db.get(Organisation, claim.recipient_id)
    if accepted:
        # Accepting needs the donor to have verified the pickup code first.
        if not claim.picked_up_at:
            raise Conflict("pickup not verified yet: ask the donor to verify your pickup code")
        # The accepted amount has to be between 1 and what was listed.
        if not (0 < accepted_quantity <= listing.quantity):
            raise Conflict("invalid accepted quantity")
        # Mark the claim completed, the listing collected, and save how many units were accepted.
        claim.status, listing.status, claim.accepted_quantity = "completed", "collected", accepted_quantity
        # Boost the recipient's track record.
        recipient.completed += 1
        # Use up some of their spare capacity (never below zero).
        recipient.capacity_remaining = max(0, recipient.capacity_remaining - accepted_quantity)
        # Remember when they last got food, for fair-share.
        recipient.last_received_at = now
        # Log the finished handover.
        append_event(db, "HANDOVER_COMPLETED", {"claim_id": claim.id, "donation_id": listing.id, "accepted_quantity": accepted_quantity})
    else:
        # Refusing needs a reason so there's a record of why.
        if not reason:
            raise Conflict("a reason is required when refusing")
        claim.status, listing.status = "rejected", "rejected"   # saying no at handover never gets penalised
        # Log the refusal with its reason.
        append_event(db, "HANDOVER_REFUSED", {"claim_id": claim.id, "donation_id": listing.id, "reason": reason})
    # Save everything.
    db.commit()
    return claim


# ---------------- timeouts and passing the offer along ----------------
def expire_due_claims(db: Session, now: datetime | None = None) -> dict:
    # Use the real time unless a test passes a fake one.
    now = now or utcnow()
    # The current first-dibs window length.
    window = offer_window_min()
    # A tally of what this sweep did, handed back at the end.
    result = {"claims_expired": 0, "offers_advanced": 0, "donations_expired": 0}

    def expire_donation(listing: Donation):
        # Mark the listing expired and clear any offer.
        listing.status, listing.offered_to, listing.offer_expires_at = "expired", None, None
        # Log it (with the quantity so it counts as "not collected").
        append_event(db, "DONATION_EXPIRED", {"donation_id": listing.id, "quantity": listing.quantity})
        # Add one to the tally.
        result["donations_expired"] += 1

    # Part 1: claims whose time ran out without a confirmed pickup.
    for claim in db.scalars(select(Claim).where(Claim.status == "reserved", Claim.expires_at <= now)).all():
        listing = db.get(Donation, claim.donation_id)
        # The claim is dead.
        claim.status = "expired"
        # Remember this recipient already had their turn.
        _remember_offered(listing, claim.recipient_id)
        if now < listing.latest_collect:
            # Food is still safe: put the listing back on the board and pass the offer to the next recipient.
            listing.status, listing.offered_to, listing.offer_expires_at = "published", None, None
            append_event(db, "CLAIM_EXPIRED", {"claim_id": claim.id, "donation_id": listing.id, "donation_status": "published"})
            advance_offer(db, listing, now, window)
        else:
            # The food's window has also closed, so the whole listing expires.
            append_event(db, "CLAIM_EXPIRED", {"claim_id": claim.id, "donation_id": listing.id, "donation_status": "expired"})
            expire_donation(listing)
        result["claims_expired"] += 1

    # Part 2: first-dibs turns that ran out unclaimed, so the offer moves to the next recipient.
    for listing in db.scalars(select(Donation).where(Donation.status == "published", Donation.offered_to.is_not(None),
                                              Donation.offer_expires_at <= now)).all():
        if now < listing.latest_collect:
            advance_offer(db, listing, now, window)
            result["offers_advanced"] += 1

    # Part 3: listings still unclaimed after their safe window closed.
    for listing in db.scalars(select(Donation).where(Donation.status == "published", Donation.latest_collect <= now)).all():
        expire_donation(listing)
    # Save everything and report the tally.
    db.commit()
    return result


# ---------------- incidents (when something feels off) ----------------
def report_incident(db: Session, reporter: Organisation, donation_id: int, note: str, now: datetime | None = None) -> Incident:
    # Use the real time unless a test passes a fake one.
    now = now or utcnow()
    # Find the listing.
    listing = db.get(Donation, donation_id)
    if not listing:
        raise NotFound("donation")
    # Only admins, the donor, or a recipient who claimed it can report a problem.
    involved = reporter.role == "admin" or listing.donor_id == reporter.id or bool(
        db.scalar(select(Claim.id).where(Claim.donation_id == listing.id, Claim.recipient_id == reporter.id)))
    if not involved:
        raise Forbidden("you are not involved in this listing")
    # Save the report.
    incident = Incident(donation_id=listing.id, reporter_id=reporter.id, note=note.strip())
    db.add(incident)
    db.flush()
    # Only listings that are still live (published/reserved) get frozen.
    froze = listing.status in ("published", "reserved")
    if froze:                                    # could be a real risk: freeze the listing and its live claim immediately, better safe than sorry
        listing.status = "frozen"
        listing.offered_to = listing.offer_expires_at = None
        # Freeze any active claim on it too.
        for claim in db.scalars(select(Claim).where(Claim.donation_id == listing.id, Claim.status == "reserved")):
            claim.status = "frozen"
    # Log the report (and whether it froze anything), then save.
    append_event(db, "INCIDENT_REPORTED", {"incident_id": incident.id, "donation_id": listing.id, "reporter_org_id": reporter.id, "froze": froze})
    db.commit()
    return incident


def resolve_incident(db: Session, incident_id: int, decision: str, note: str = "", now: datetime | None = None) -> Incident:
    # Use the real time unless a test passes a fake one.
    now = now or utcnow()
    # Find the incident.
    incident = db.get(Incident, incident_id)
    if not incident:
        raise NotFound("incident")
    # Can't resolve the same incident twice.
    if incident.status != "open":
        raise Conflict("incident already resolved")
    # The admin must choose one of two outcomes.
    if decision not in ("release", "discard"):
        raise Conflict("decision must be release or discard")
    # Look up the listing the incident is about.
    listing = db.get(Donation, incident.donation_id)
    if listing.status == "frozen":
        # Find the claims that were frozen along with it.
        frozen = db.scalars(select(Claim).where(Claim.donation_id == listing.id, Claim.status == "frozen")).all()
        if decision == "discard":
            # Throw the food away: listing discarded, frozen claims closed.
            listing.status = "discarded"
            for frozen_claim in frozen:
                frozen_claim.status = "closed"
            # Log it so the units count as "not collected".
            append_event(db, "DONATION_DISCARDED", {"donation_id": listing.id, "quantity": listing.quantity})
        else:
            # Release: the old claims lapse (and those recipients are marked as having had their turn).
            for frozen_claim in frozen:
                frozen_claim.status = "expired"
                _remember_offered(listing, frozen_claim.recipient_id)
            if now < listing.latest_collect:
                # Still safe: back on the board and offer it again.
                listing.status = "published"
                advance_offer(db, listing, now, offer_window_min())
            else:
                # Too late: the window closed while it was frozen.
                listing.status = "expired"
                append_event(db, "DONATION_EXPIRED", {"donation_id": listing.id, "quantity": listing.quantity})
    # Record the admin's decision on the incident itself (the note is cut to 500 characters).
    incident.status, incident.resolution, incident.resolution_note, incident.resolved_at = "resolved", decision, note[:500], now
    # Log the resolution, then save.
    append_event(db, "INCIDENT_RESOLVED", {"incident_id": incident.id, "donation_id": listing.id, "decision": decision})
    db.commit()
    return incident


# ---------------- reporting (the numbers) ----------------
def impact(db: Session) -> dict:
    """Only verified outcomes, recalculated from the event log."""
    # Start every counter at zero.
    result = {"listed_units": 0, "collected_units": 0, "uncollected_units": 0,
           "handovers_completed": 0, "handovers_refused": 0, "incidents_reported": 0}
    # Walk through the whole log in order and add things up.
    for event in db.scalars(select(AuditEvent).order_by(AuditEvent.id)):
        # The event's details were saved as JSON text, so turn them back into a dictionary.
        payload = json.loads(event.payload)
        if event.type == "DONATION_PUBLISHED":
            # Food was listed.
            result["listed_units"] += payload["quantity"]
        elif event.type == "HANDOVER_COMPLETED":
            # Food was delivered: count the handover and the units accepted.
            result["handovers_completed"] += 1
            result["collected_units"] += payload["accepted_quantity"]
        elif event.type == "HANDOVER_REFUSED":
            # Recipient said no at the door.
            result["handovers_refused"] += 1
        elif event.type in ("DONATION_EXPIRED", "DONATION_DISCARDED"):
            # Food went to waste.
            result["uncollected_units"] += payload["quantity"]
        elif event.type == "INCIDENT_REPORTED":
            # Someone reported a problem.
            result["incidents_reported"] += 1
    return result


def fairness(db: Session) -> dict:
    # Count how many offers each recipient has received (a Counter returns 0 for anyone not counted yet).
    offers = Counter()
    for event in db.scalars(select(AuditEvent).where(AuditEvent.type == "OFFER_MADE")):
        offers[json.loads(event.payload)["recipient_id"]] += 1
    # One row per recipient, filled in below.
    rows = []
    for org in db.scalars(select(Organisation).where(Organisation.role == "recipient").order_by(Organisation.id)):
        # How many of this recipient's claims ended in each status.
        status_counts = Counter(c.status for c in db.scalars(select(Claim).where(Claim.recipient_id == org.id)))
        # Total units they actually received (0 if none).
        units = db.scalar(select(func.coalesce(func.sum(Claim.accepted_quantity), 0)).where(Claim.recipient_id == org.id, Claim.status == "completed"))
        rows.append({"org_id": org.id, "name": org.name, "verified": org.verified, "offers": offers[org.id], "claims": sum(status_counts.values()),
                     "completed": status_counts["completed"], "expired": status_counts["expired"], "refused": status_counts["rejected"], "collected_units": int(units)})
    # Total units received by everyone.
    total = sum(row["collected_units"] for row in rows)
    for row in rows:
        # Each recipient's percentage of the total (None if nothing was collected yet).
        row["share_pct"] = round(100 * row["collected_units"] / total) if total else None
    # The biggest single share.
    top = max((row["share_pct"] or 0 for row in rows), default=0)
    # Warn if one organisation got more than half, but only when there are at least 3 recipients and some food collected.
    return {"rows": rows, "total_collected": total, "concentration_warning": bool(total and len(rows) >= 3 and top > 50)}
