# The tamper-evident log. Every event stores the fingerprint of the one before it, like links in a chain.
import hashlib  # makes the SHA-256 fingerprints
import json  # turns event details into text
from sqlalchemy import select  # for asking the database questions
from sqlalchemy.orm import Session  # the type of a database conversation
from .tables import AuditEvent  # the table that stores the log
from .database import utcnow  # our UTC clock

# The "previous hash" of the very first event: just 64 zeros since there's nothing before it.
GENESIS = "0" * 64


def canonical(payload: dict) -> str:
    # Turn the details into JSON with sorted keys and no extra spaces, so the same data always gives the exact same text (and the same hash).
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(previous_hash: str, timestamp: str, event_type: str, payload: str) -> str:
    # The fingerprint of an event covers the previous fingerprint plus this event's own time, type and details.
    return hashlib.sha256(f"{previous_hash}|{timestamp}|{event_type}|{payload}".encode()).hexdigest()


def append_event(db: Session, event_type: str, payload: dict) -> AuditEvent:
    """Add a new log entry inside the caller's transaction. prev_hash is UNIQUE, so if two entries
    try to chain off the same one, it fails loudly instead of quietly forking
    (on Postgres, also grab an advisory lock around this call)."""
    # Find the newest event so we know what to chain onto.
    last = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(1)).first()
    # Chain onto it, or onto the all-zeros start if the log is empty.
    previous_hash = last.hash if last else GENESIS
    # Stamp the event with the current time.
    timestamp = utcnow().isoformat()
    # Turn the details into the neat canonical text.
    body = canonical(payload)
    # Build the new log row, including its own fingerprint.
    event = AuditEvent(ts=timestamp, type=event_type, payload=body, prev_hash=previous_hash, hash=_hash(previous_hash, timestamp, event_type, body))
    # Add it to the session and push it to the database (without committing, the caller does that).
    db.add(event)
    db.flush()
    return event


def verify_chain(db: Session) -> dict:
    # Start from the all-zeros beginning and walk forward.
    previous_hash = GENESIS
    # How many events we've checked so far.
    count = 0
    # Go through every event in order.
    for event in db.scalars(select(AuditEvent).order_by(AuditEvent.id)):
        count += 1
        # If the link to the previous event is wrong, or the fingerprint doesn't match the contents, someone edited the log.
        if event.prev_hash != previous_hash or event.hash != _hash(previous_hash, event.ts, event.type, event.payload):
            return {"ok": False, "checked": count, "broken_at_event_id": event.id, "head": None}
        # This event checks out, so it becomes the "previous" one for the next loop.
        previous_hash = event.hash
    # Whole chain is fine; "head" is the latest fingerprint.
    return {"ok": True, "checked": count, "broken_at_event_id": None, "head": previous_hash}
