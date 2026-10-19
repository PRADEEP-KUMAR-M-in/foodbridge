# The web server: every URL the website and phone app talk to lives in this file.
import asyncio  # lets the background timer run alongside normal requests
import threading  # used for the simulator "slots" limit
import os  # read settings and build file paths
import re  # pattern matching (username check)
from contextlib import asynccontextmanager  # helper for the start-up / shut-down block
from datetime import datetime, timezone  # date+time and timezones
from typing import Literal  # lets us say "this value must be exactly one of these words"
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request  # the web framework pieces
from fastapi.staticfiles import StaticFiles  # serves the website files (web/ folder)
from pydantic import BaseModel, Field  # describes and checks the data people send us
from sqlalchemy import func, select  # database counting and querying
from sqlalchemy.orm import Session  # the type of a database conversation
from . import security, demo_data, tracking, speak_to_list, matching, qr, actions, simulator  # all our other modules
from .proof_chain import verify_chain  # checks the tamper-evident log
from .database import SessionLocal, init_db, utcnow  # database session factory, table creator, and clock
from .tables import Claim, Donation, Incident, Organisation  # the tables we query here
from .safe_window import load_rules  # the food-safety timing rules


def _expire_once():
    # Open a database session, run one sweep of "expire old claims / pass offers along", and close it again.
    with SessionLocal() as s:
        return actions.expire_due_claims(s)


async def _expiry_loop(interval: int):
    # Runs forever in the background.
    while True:
        # Wait between sweeps.
        await asyncio.sleep(interval)
        try:
            # Do the sweep in a separate thread so the web server doesn't freeze while it works.
            await asyncio.to_thread(_expire_once)
        except Exception as e:                       # keep this loop alive no matter what (it should never crash)
            print(f"[foodbridge] expiry loop error: {e}", flush=True)


@asynccontextmanager
async def lifespan(_):
    # Everything before "yield" runs when the server starts; everything after runs when it stops.
    # Make sure the tables exist.
    init_db()
    if os.getenv("FOODBRIDGE_AUTOSEED") == "1":     # hosted demo: free hosts wipe files on restart, so we just recreate the demo data
        with SessionLocal() as s:
            seeded = demo_data.seed_demo(s, os.getenv("FOODBRIDGE_ADMIN_PASSWORD"), os.getenv("FOODBRIDGE_DEMO_PASSWORD", "demo-pass-123"))
            if seeded and seeded["admin_generated"]:
                # Nobody set an admin password, so print the random one we made (otherwise nobody could ever log in as admin).
                print(f"[foodbridge] FOODBRIDGE_ADMIN_PASSWORD not set; generated admin password: {seeded['admin_password']}", flush=True)
    try:
        # How often (in seconds) the background sweep runs; default 30.
        interval = int(os.getenv("FOODBRIDGE_EXPIRY_INTERVAL", "30"))
    except ValueError:
        # Not a number: use 30.
        interval = 30
    # Start the background timer (an interval of 0 or less switches it off).
    task = asyncio.create_task(_expiry_loop(interval)) if interval > 0 else None
    # The app runs while we pause here.
    yield
    # Server is shutting down: stop the timer.
    if task:
        task.cancel()


# Create the web app itself.
app = FastAPI(title="FOODBRIDGE", version="0.7", lifespan=lifespan)
# Load the safe-window rules once when the server starts.
RULES = load_rules()
# Which food types and storage types are actually allowed (rules with no time limit don't count).
KNOWN_CATEGORIES = {category for (category, _), hours in RULES.items() if hours is not None}
KNOWN_STORAGE = {storage for (_, storage), hours in RULES.items() if hours is not None}


@app.middleware("http")
async def security_headers(request, call_next):
    # This runs on every request: let the request through, then add safety headers to the reply.
    resp = await call_next(request)
    resp.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
                         "Permissions-Policy": "microphone=(self), geolocation=(self), camera=(self)"})
    if request.url.path.startswith("/api/"):
        # API answers should never be saved by the browser or a proxy.
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/healthz")
def healthz():
    # A simple "are you alive?" check for the hosting service.
    return {"ok": True}


def get_session():
    # Gives each request its own database session and closes it afterwards.
    with SessionLocal() as s:
        yield s


def guarded(fn, *a, **kw):
    # Runs an action and turns our own errors into proper web errors (404 / 403 / 409).
    try:
        return fn(*a, **kw)
    except actions.NotFound as e:
        raise HTTPException(404, str(e))
    except actions.Forbidden as e:
        raise HTTPException(403, str(e))
    except actions.Conflict as e:
        raise HTTPException(409, str(e))


def current_org(authorization: str | None = Header(None), db: Session = Depends(get_session)) -> Organisation:
    # Works out who is logged in from the "Authorization: Bearer <token>" header.
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "login required")
    # Cut off the "Bearer " part (7 characters) and read the organisation id out of the token.
    oid = security.read_token(authorization[7:])
    # Look the organisation up (only if the token was valid).
    org = db.get(Organisation, oid) if oid else None
    if not org or not org.active:
        raise HTTPException(401, "invalid or expired session")
    return org


def need(*roles):
    # Builds a "you must be one of these roles" checker that routes can plug in.
    def dep(org: Organisation = Depends(current_org)) -> Organisation:
        if org.role not in roles:
            raise HTTPException(403, "not allowed for your role")
        return org
    return dep


def iso(dt: datetime | None):
    return dt.isoformat() + "Z" if dt else None      # saved as UTC with no timezone tag stuck on it


def naive_utc(dt: datetime) -> datetime:
    # If the time has a timezone, convert it to UTC and drop the label; otherwise leave it as it is.
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


# ---------- the shapes of data people send us (pydantic checks them automatically) ----------
class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)  # organisation name
    role: Literal["donor", "recipient"]  # you can't register as admin
    username: str  # login name (checked more closely in register())
    password: str = Field(min_length=8, max_length=200)  # at least 8 characters
    lat: float = Field(ge=-90, le=90)  # latitude has to be a real one
    lon: float = Field(ge=-180, le=180)  # longitude has to be a real one
    accepts_categories: list[str] = []  # food types a recipient accepts
    storage_available: list[str] = []  # storage types a recipient has
    capacity_remaining: int = Field(0, ge=0, le=100000)  # how many units they can take


class LoginIn(BaseModel):
    username: str  # who's logging in
    password: str  # their password


class IntakeIn(BaseModel):
    text: str = Field(min_length=3, max_length=500)  # the sentence the donor spoke or typed
    tz_offset_minutes: int = Field(0, ge=-840, le=840)     # the donor's phone timezone (from JavaScript's Date.getTimezoneOffset())


class DonationIn(BaseModel):
    category: str  # type of food
    storage: str  # how it's stored
    quantity: int  # how many units
    prepared_at: datetime  # when it was made
    donor_deadline: datetime  # the donor's own latest pickup time
    declaration: str = Field("", max_length=300)  # optional ingredients / allergens note


class HandoverIn(BaseModel):
    accepted: bool  # did the recipient accept the food?
    accepted_quantity: int = 0  # how many units they accepted
    reason: str | None = None  # why they refused (required when refusing)


class PickupIn(BaseModel):
    code: str = Field(min_length=3, max_length=60)  # the pickup code typed or scanned


class IncidentIn(BaseModel):
    note: str = Field(min_length=3, max_length=1000)  # what went wrong


class ResolveIn(BaseModel):
    decision: Literal["release", "discard"]  # the admin's decision
    note: str = Field("", max_length=500)  # optional explanation


# ---------- helpers that turn database objects into plain data for the website ----------
def org_json(org: Organisation):
    # Only the safe public fields (never the password hash).
    return {"id": org.id, "name": org.name, "role": org.role, "username": org.username, "verified": org.verified,
            "capacity_remaining": org.capacity_remaining}


def match_json(match):
    # One recipient's match result; the explanation and reasons only exist if they're eligible.
    return {"org_id": match.org_id, "name": match.name, "score": match.score, "factors": match.factors,
            "eta_min": round(match.eta_min, 1), "slack_min": round(match.slack_min, 1),
            "explanation": matching.explain(match) if match.eligible else None, "reasons": match.reasons if match.eligible else [],
            "excluded_reasons": match.excluded_reasons}


def donation_json(listing: Donation, donor_name: str):
    # The basic public details of a listing.
    return {"id": listing.id, "donor": donor_name, "category": listing.category, "storage": listing.storage,
            "quantity": listing.quantity, "status": listing.status, "latest_collect": iso(listing.latest_collect)}


# ---------- public (anyone can see these) ----------
@app.get("/api/rules")
def list_rules():
    """Only the allowed category + storage combos (we don't show the hold-time numbers)."""
    return [{"category": category, "storage": storage} for (category, storage), hours in RULES.items() if hours is not None]


@app.get("/api/public/stats")
def public_stats(db: Session = Depends(get_session)):
    """Anonymous totals for the landing page: just counts, no names, no listings."""
    # Overall numbers recalculated from the log.
    totals = actions.impact(db)
    # Small helper: count approved, active organisations with a given role.
    n = lambda role: db.scalar(select(func.count()).select_from(Organisation).where(
        Organisation.role == role, Organisation.verified.is_(True), Organisation.active.is_(True))) or 0
    return {"collected_units": totals["collected_units"], "handovers_completed": totals["handovers_completed"],
            "donors": n("donor"), "recipients": n("recipient")}


@app.post("/api/register")
def register(body: RegisterIn, db: Session = Depends(get_session)):
    # Usernames are case-insensitive, so we always lowercase them.
    username = body.username.strip().lower()
    # 3-40 characters made of letters, digits and . _ -
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        raise HTTPException(422, "username: 3-40 characters, letters, digits . _ -")
    # No two organisations can share a username.
    if db.scalars(select(Organisation).where(Organisation.username == username)).first():
        raise HTTPException(409, "username already taken")
    # Only food and storage types that exist in the rules are allowed.
    if not set(body.accepts_categories) <= KNOWN_CATEGORIES or not set(body.storage_available) <= KNOWN_STORAGE:
        raise HTTPException(422, "unknown category or storage type")
    # Create the organisation (lists are saved as comma-separated text, and the password is saved scrambled).
    org = actions.create_org(db, name=body.name.strip(), role=body.role, username=username, password_hash=security.hash_password(body.password),
                           lat=body.lat, lon=body.lon, accepts_categories=",".join(body.accepts_categories),
                           storage_available=",".join(body.storage_available), capacity_remaining=body.capacity_remaining)
    return {"id": org.id, "verified": org.verified}      # new orgs have to wait for an admin to approve them


@app.post("/api/auth/login")
def login(body: LoginIn, db: Session = Depends(get_session)):
    # Same lowercasing as when registering.
    username = body.username.strip().lower()
    # Too many wrong tries recently? Make them wait.
    if security.is_locked_out(username):
        raise HTTPException(429, "too many attempts, try again in a minute")
    # Look the user up.
    org = db.scalars(select(Organisation).where(Organisation.username == username)).first()
    # Check the password (against a dummy hash if the user doesn't exist, so timing doesn't reveal which usernames are real).
    ok = security.check_password(body.password, org.password_hash if org and org.password_hash else security.DUMMY_HASH)
    if not (ok and org and org.password_hash and org.active):
        # Wrong login: count it, and give a vague error that doesn't say which part was wrong.
        security.record_failed_login(username)
        raise HTTPException(401, "wrong username or password")
    # Right login: clear their failure count and hand over a token.
    security.forget_failed_logins(username)
    return {"token": security.make_token(org.id), "org": org_json(org)}


# ---------- any logged-in user (just be logged in, that's it) ----------
@app.get("/api/me")
def me(org: Organisation = Depends(current_org)):
    # Tells the website who's logged in.
    return org_json(org)


@app.get("/api/audit/verify")
def audit_verify(_: Organisation = Depends(current_org), db: Session = Depends(get_session)):
    # Runs the tamper check over the whole log.
    return verify_chain(db)


@app.get("/api/impact")
def impact(_: Organisation = Depends(current_org), db: Session = Depends(get_session)):
    # The headline impact numbers.
    return actions.impact(db)


@app.post("/api/donations/{donation_id}/incident")
def report_incident(donation_id: int, body: IncidentIn, org: Organisation = Depends(current_org), db: Session = Depends(get_session)):
    # File the report (the action itself checks the person is involved).
    incident = guarded(actions.report_incident, db, org, donation_id, body.note)
    # Tell the website the new incident id and what state the listing is in now.
    return {"incident_id": incident.id, "donation_status": db.get(Donation, donation_id).status}


# ---------- admin only (boss mode) ----------
@app.get("/api/orgs")
def list_orgs(_: Organisation = Depends(need("admin")), db: Session = Depends(get_session)):
    # Every organisation, for the Approvals tab.
    return [org_json(org) for org in db.scalars(select(Organisation).order_by(Organisation.id))]


@app.post("/api/orgs/{org_id}/verify")
def verify(org_id: int, _: Organisation = Depends(need("admin")), db: Session = Depends(get_session)):
    # Approve an organisation.
    guarded(actions.verify_org, db, org_id)
    return {"ok": True}


@app.post("/api/maintenance/expire")
def expire(_: Organisation = Depends(need("admin")), db: Session = Depends(get_session)):
    # Lets an admin trigger the "expire old claims" sweep by hand.
    return guarded(actions.expire_due_claims, db)


@app.get("/api/incidents")
def list_incidents(_: Organisation = Depends(need("admin")), db: Session = Depends(get_session)):
    # A lookup from organisation id to name, so we can show names instead of numbers.
    names = {org.id: org.name for org in db.scalars(select(Organisation))}
    # The list we'll send back.
    out = []
    # Latest 50 incidents, open ones first.
    for incident in db.scalars(select(Incident).order_by(Incident.status.desc(), Incident.id.desc()).limit(50)):
        # The listing each incident is about.
        listing = db.get(Donation, incident.donation_id)
        out.append({"id": incident.id, "status": incident.status, "note": incident.note, "reporter": names.get(incident.reporter_id), "donor": names.get(listing.donor_id),
                    "category": listing.category, "quantity": listing.quantity, "donation_status": listing.status, "created_at": iso(incident.created_at),
                    "resolution": incident.resolution})
    return out


@app.post("/api/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: int, body: ResolveIn, _: Organisation = Depends(need("admin")), db: Session = Depends(get_session)):
    # The admin releases or discards the food.
    incident = guarded(actions.resolve_incident, db, incident_id, body.decision, body.note)
    return {"status": incident.status, "resolution": incident.resolution}


@app.get("/api/fairness")
def fairness(_: Organisation = Depends(need("admin")), db: Session = Depends(get_session)):
    # Who has been offered and received how much.
    return actions.fairness(db)


# ---------- donors (the ones giving food) ----------
@app.post("/api/intake/parse")
def intake_parse(body: IntakeIn, _: Organisation = Depends(need("donor"))):
    """Speak-to-List draft. It never publishes anything: the donor checks it and confirms first."""
    # Turn the sentence into a draft listing (the rules are passed in so it can check the food + storage combo).
    return speak_to_list.parse_listing(body.text, utcnow(), body.tz_offset_minutes, RULES)


@app.post("/api/donations")
def publish(body: DonationIn, donor: Organisation = Depends(need("donor")), db: Session = Depends(get_session)):
    # Turn the incoming data into a plain dictionary.
    data = body.model_dump()
    # Make sure both times are plain UTC times (no timezone label).
    data["prepared_at"], data["donor_deadline"] = naive_utc(body.prepared_at), naive_utc(body.donor_deadline)
    # Publish the listing for the logged-in donor.
    listing = guarded(actions.publish_donation, db, RULES, donor_id=donor.id, **data)
    return {"id": listing.id, "latest_collect": iso(listing.latest_collect)}


@app.get("/api/donations")
def list_donations(org: Organisation = Depends(need("donor", "admin")), db: Session = Depends(get_session)):
    # Latest 50 listings, newest first.
    q = select(Donation).order_by(Donation.id.desc()).limit(50)
    if org.role == "donor":
        # Donors only see their own listings (admins see everything).
        q = q.where(Donation.donor_id == org.id)
    # Organisation id -> name lookup.
    names = {org.id: org.name for org in db.scalars(select(Organisation))}
    now = utcnow()
    items = []
    for listing in db.scalars(q):
        item = donation_json(listing, names[listing.donor_id])
        if actions.active_offer(listing, now):
            # Show who currently has first dibs and until when.
            item["offered_to"] = names.get(listing.offered_to)
            item["offer_expires_at"] = iso(listing.offer_expires_at)
        # The most recent live or finished claim on this listing, if any.
        claim = db.scalars(select(Claim).where(Claim.donation_id == listing.id, Claim.status.in_(("reserved", "frozen", "completed")))
                      .order_by(Claim.id.desc())).first()
        if claim:
            item["claim"] = {"recipient": names.get(claim.recipient_id), "status": claim.status, "picked_up": bool(claim.picked_up_at)}
        items.append(item)
    # server_now lets the website show countdowns without trusting the phone's clock.
    return {"server_now": iso(now), "items": items}


@app.get("/api/donations/{donation_id}/matches")
def matches(donation_id: int, org: Organisation = Depends(need("donor", "admin")), db: Session = Depends(get_session)):
    listing = db.get(Donation, donation_id)
    if not listing:
        raise HTTPException(404, "donation")
    if org.role == "donor" and listing.donor_id != org.id:
        # A donor can only look at matches for their own listing.
        raise HTTPException(403, "not your listing")
    # Rank the recipients for this listing.
    matches = guarded(actions.matches_for, db, donation_id)
    return {"ranked": [match_json(r) for r in matches["ranked"]], "excluded": [match_json(r) for r in matches["excluded"]]}


@app.post("/api/pickups/verify")
def verify_pickup(body: PickupIn, donor: Organisation = Depends(need("donor")), db: Session = Depends(get_session)):
    # The donor confirms the pickup code the recipient showed them.
    return guarded(actions.verify_pickup, db, donor.id, body.code)


# ---------- recipients (the ones getting food) ----------
@app.get("/api/feed")
def feed(recipient: Organisation = Depends(need("recipient")), db: Session = Depends(get_session)):
    now = utcnow()
    out = []
    # Every published listing, soonest deadline first.
    for listing in db.scalars(select(Donation).where(Donation.status == "published").order_by(Donation.latest_collect)):
        # Skip listings whose window has already closed.
        if listing.latest_collect <= now:
            continue
        donor = db.get(Organisation, listing.donor_id)
        # Check whether this recipient can take it (and why not, if they can't).
        result = matching.evaluate(listing, donor, recipient, now)
        item = {**donation_json(listing, donor.name), "eligible": result.eligible, "eta_min": round(result.eta_min, 1),
                "excluded_reasons": result.excluded_reasons, "explanation": matching.explain(result) if result.eligible else None,
                "reasons": result.reasons if result.eligible else []}
        if actions.active_offer(listing, now):
            # Tell them if the offer is for them, or for somebody else.
            item["offer"] = "you" if listing.offered_to == recipient.id else "other"
            item["offer_expires_at"] = iso(listing.offer_expires_at)
        out.append(item)
    return {"server_now": iso(now), "items": out}


@app.get("/api/claims")
def my_claims(recipient: Organisation = Depends(need("recipient")), db: Session = Depends(get_session)):
    # This recipient's latest 30 claims, newest first.
    rows = db.scalars(select(Claim).where(Claim.recipient_id == recipient.id).order_by(Claim.id.desc()).limit(30)).all()
    items = []
    for claim in rows:
        listing = db.get(Donation, claim.donation_id)
        item = {**donation_json(listing, db.get(Organisation, listing.donor_id).name),       # the claim's own fields have to win over the listing's
                "claim_id": claim.id, "status": claim.status, "donation_status": listing.status,
                "expires_at": iso(claim.expires_at), "picked_up": bool(claim.picked_up_at)}
        if claim.status == "reserved":
            # Active claims also show the pickup code and its QR picture.
            code = actions.pickup_code(claim.id)
            item["pickup_code"], item["qr_svg"] = code, qr.qr_svg(code)
        items.append(item)
    return {"server_now": iso(utcnow()), "items": items}


@app.post("/api/donations/{donation_id}/claim")
def claim(donation_id: int, recipient: Organisation = Depends(need("recipient")), db: Session = Depends(get_session)):
    # Call dibs on a listing and tell the website the new claim's id.
    return {"claim_id": guarded(actions.claim, db, donation_id, recipient.id).id}


@app.post("/api/claims/{claim_id}/handover")
def handover(claim_id: int, body: HandoverIn, recipient: Organisation = Depends(need("recipient")), db: Session = Depends(get_session)):
    claim = db.get(Claim, claim_id)
    if not claim:
        raise HTTPException(404, "claim")
    if claim.recipient_id != recipient.id:
        # You can only hand over your own claim.
        raise HTTPException(403, "not your claim")
    # Accept or refuse the food, and report the claim's new status.
    return {"status": guarded(actions.complete_handover, db, claim_id, **body.model_dump()).status}


# ---------- v0.7 stuff: live tracking, passport, impact details, rescue planner ----------
def can_view_donation(org: Organisation, listing: Donation, db: Session) -> bool:
    # Admins and the donor who posted it can always look.
    if org.role == "admin" or listing.donor_id == org.id:
        return True
    # Anyone else can only look if they've made a claim on it.
    return bool(db.scalar(select(Claim.id).where(Claim.donation_id == listing.id, Claim.recipient_id == org.id)))


def load_visible_donation(donation_id: int, org: Organisation, db: Session) -> Donation:
    listing = db.get(Donation, donation_id)
    if not listing:
        raise HTTPException(404, "donation")
    if not can_view_donation(org, listing, db):
        raise HTTPException(403, "not involved in this listing")
    return listing


@app.get("/api/live")
def live_board(org: Organisation = Depends(need("donor", "admin")), db: Session = Depends(get_session)):
    """Every recent listing with its stage, outcome and risk flags. The Live tab keeps asking for this."""
    # Latest 25 listings, newest first.
    q = select(Donation).order_by(Donation.id.desc()).limit(25)
    if org.role == "donor":
        # Donors only see their own.
        q = q.where(Donation.donor_id == org.id)
    # Organisation id -> name lookup.
    names = {org.id: org.name for org in db.scalars(select(Organisation))}
    now = utcnow()
    # For each listing: its basic details plus the tracking timeline (leaving out fields that would repeat the basics).
    items = [{**donation_json(listing, names[listing.donor_id]), **{k: v for k, v in tracking.timeline(db, listing, now).items() if k not in ("donation_id", "status", "latest_collect")}}
             for listing in db.scalars(q)]
    return {"server_now": iso(now), "items": items}


@app.get("/api/donations/{donation_id}/timeline")
def donation_timeline(donation_id: int, org: Organisation = Depends(current_org), db: Session = Depends(get_session)):
    # Only people involved in the listing can see its timeline.
    listing = load_visible_donation(donation_id, org, db)
    return {"server_now": iso(utcnow()), **tracking.timeline(db, listing)}


@app.get("/api/donations/{donation_id}/passport")
def donation_passport(donation_id: int, request: Request, org: Organisation = Depends(current_org), db: Session = Depends(get_session)):
    # Same access rule as the timeline; the website's address is passed in so the passport can include a link/QR to itself.
    listing = load_visible_donation(donation_id, org, db)
    return tracking.passport(db, listing, str(request.base_url))


@app.get("/api/impact/detail")
def impact_detail(_: Organisation = Depends(current_org), db: Session = Depends(get_session)):
    # The detailed "Proof" tab numbers.
    return tracking.impact_detail(db)


@app.get("/api/planner")
def planner(vehicle_capacity: int | None = Query(None, ge=0, le=100000), recipient: Organisation = Depends(need("recipient")),
            db: Session = Depends(get_session)):
    if not recipient.verified:
        # Unapproved organisations can't plan pickups.
        raise HTTPException(403, "your organisation must be approved first")
    return {"server_now": iso(utcnow()), **tracking.plan_route(db, recipient, vehicle_capacity=vehicle_capacity)}


class SimIn(BaseModel):
    # The adjustable settings for the what-if simulator (each one has sensible limits so nobody can overload the server).
    runs: int = Field(100, ge=10, le=200)  # how many simulated weeks
    seed: int = Field(1, ge=0, le=1_000_000)  # random seed (same seed = same answers)
    days: int = Field(7, ge=1, le=14)  # days in each simulated week
    recipients: int = Field(8, ge=2, le=30)  # how many receiving organisations
    listings_per_day: float = Field(3.0, ge=0.5, le=10)  # average listings per day
    p_respond: float = Field(0.70, ge=0.05, le=1)  # chance an organisation answers at all
    median_delay_min: float = Field(20.0, ge=1, le=120)  # typical minutes to answer
    p_noshow_mean: float = Field(0.12, ge=0, le=0.5)  # average no-show chance
    p_blind_yes: float = Field(0.30, ge=0, le=1)  # chance of saying yes to a misfit broadcast
    offer_window_min: float = Field(2.0, ge=1, le=30)  # first-dibs minutes


_SIM_SLOTS = threading.BoundedSemaphore(2)      # the simulator eats CPU, so we cap it to keep the public demo from lagging


@app.post("/api/simulate")
def simulate(body: SimIn, _: Organisation = Depends(current_org)):
    """What-if model: today's broadcast-and-wait way vs FOODBRIDGE. It's a MODEL, not real-world results."""
    # Try to grab one of the 2 simulator slots without waiting; if both are busy, tell the user to try again.
    if not _SIM_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "simulator is busy, try again in a few seconds")
    try:
        # Build the settings (runs and seed are passed separately, so they're left out here).
        params = simulator.Params(**{k: v for k, v in body.model_dump().items() if k not in ("runs", "seed")})
        return simulator.simulate(params, runs=body.runs, seed=body.seed)
    finally:
        # Always give the slot back, even if the simulation crashed.
        _SIM_SLOTS.release()


# Where the website files live (the web/ folder next to app/).
WEB_DIR = os.path.join(os.path.dirname(__file__), "..", "web")
if os.path.isdir(WEB_DIR):
    # Serve the website at "/" (mounted last so the /api routes above win first).
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
