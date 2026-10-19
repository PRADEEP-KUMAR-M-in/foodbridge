"""Fake demo data for showing the app off. Safe to run twice: if any organisation already exists, it does nothing."""
import secrets  # for making a random admin password when none is given
from datetime import timedelta  # lengths of time, for "60 minutes ago" etc.
from sqlalchemy import func, select  # func.count lets us count rows, select asks questions
from sqlalchemy.orm import Session  # the type of a database conversation
from . import actions  # the real app actions (so demo data goes through the same rules as real data)
from .security import hash_password  # scrambles passwords
from .database import utcnow  # our UTC clock
from .tables import Organisation  # the table we check to see if data already exists
from .safe_window import load_rules  # the food-safety timing rules


def seed_demo(db: Session, admin_password: str | None = None, demo_password: str = "demo-pass-123"):
    # If there's already any organisation in the database, do nothing (that's what makes it safe to run twice).
    if db.scalar(select(func.count()).select_from(Organisation)):
        return None
    # Remember whether we had to invent the admin password, so the caller can tell the user.
    generated = admin_password is None
    # Use the given admin password, or make a random one.
    admin_password = admin_password or secrets.token_urlsafe(12)
    # One "now" so all the demo times line up.
    now = utcnow()
    # Create the admin account.
    actions.create_org(db, name="FOODBRIDGE Admin", role="admin", lat=0, lon=0, verified=True,
                       username="admin", password_hash=hash_password(admin_password))
    # Create the demo bakery (a donor) in Chennai.
    bakery = actions.create_org(db, name="Demo Bakery (fictional)", role="donor", lat=13.0827, lon=80.2707, verified=True,
                                username="bakery", password_hash=hash_password(demo_password))

    def add_recipient(name, user, lat, lon, **kw):
        # Sensible defaults every demo recipient shares...
        base = dict(name=name, role="recipient", lat=lat, lon=lon, verified=True, username=user, password_hash=hash_password(demo_password),
                    accepts_categories="packaged_bakery", storage_available="ambient", capacity_remaining=60)
        # ...which any extra settings passed in can override.
        base.update(kw)
        return actions.create_org(db, **base)
    # Shelter A has a good track record (9 of 10) and last got food yesterday.
    add_recipient("Demo Shelter A (fictional)", "shelter-a", 13.0900, 80.2800, completed=9, attempts=10, last_received_at=now - timedelta(days=1))
    # Shelter B is brand new, with no history.
    add_recipient("Demo Shelter B (fictional)", "shelter-b", 13.0950, 80.2650)
    # Kitchen C only has chilled storage, so it can't take the demo's room-temperature bread (good for showing an exclusion).
    add_recipient("Demo Kitchen C (fictional, no storage)", "kitchen-c", 13.0850, 80.2750, storage_available="chilled")
    # The bakery posts one listing: 20 packs, packed an hour ago, collect within 2 hours.
    first_listing = actions.publish_donation(db, load_rules(), bakery.id, "packaged_bakery", "ambient", 20,
                                 now - timedelta(minutes=60), now + timedelta(hours=2), offer_window=0)   # this demo listing is open to everyone right away (no first-dibs wait)
    # two more fake donors nearby so the Rescue Plan has an actual route to show off
    for name, user, lat, lon, quantity, left in (("Demo Sweet Shop (fictional)", "sweetshop", 13.0870, 80.2760, 15, 45),
                                            ("Demo Canteen (fictional)", "canteen", 13.0930, 80.2690, 12, 90)):
        # Create each extra donor...
        donor = actions.create_org(db, name=name, role="donor", lat=lat, lon=lon, verified=True,
                                username=user, password_hash=hash_password(demo_password))
        # ...and give each one a listing that closes after "left" minutes (the sweet shop also gets an allergen note).
        actions.publish_donation(db, load_rules(), donor.id, "packaged_bakery", "ambient", quantity,
                                 now - timedelta(minutes=30), now + timedelta(minutes=left), offer_window=0,
                                 declaration="Contains wheat and milk. Sealed packs." if user == "sweetshop" else "")
    # Tell the caller the admin password and which listing was created first.
    return {"admin_password": admin_password, "admin_generated": generated, "donation_id": first_listing.id}
