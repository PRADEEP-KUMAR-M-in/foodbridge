import threading  # lets us fire several claims at the exact same moment
from datetime import timedelta  # lengths of time
import pytest  # the testing tool
from sqlalchemy import text  # lets a test run raw SQL (used to simulate someone tampering with the log)
from sqlalchemy.orm import sessionmaker  # makes database sessions
from app import matching, actions  # the ranking rules and the main actions
from app.proof_chain import verify_chain  # checks the tamper-evident log
from app.database import make_engine, init_db, utcnow  # build a test database, create tables, and the clock
from app.safe_window import load_rules, latest_collect, RuleError  # the timing rules and their error

# One fixed "now" for the whole file, so every test works with the same moment.
NOW = utcnow()


@pytest.fixture()
def env(tmp_path):
    # A fresh, empty test database in a temporary folder (deleted after the test).
    db_engine = make_engine(f"sqlite:///{tmp_path/'t.db'}")
    init_db(db_engine)
    make_session = sessionmaker(bind=db_engine, expire_on_commit=False)
    db = make_session()
    # The safe-window rules.
    rules = load_rules()
    # One verified bakery to post food.
    donor = actions.create_org(db, name="Bakery", role="donor", lat=13.08, lon=80.27, verified=True)
    def make_recipient(name, lat, **kw):
        # Defaults for a normal test recipient: approved, takes bakery food, has room-temperature storage, 100 units of space.
        base = dict(name=name, role="recipient", lat=lat, lon=80.27, verified=True,
                    accepts_categories="packaged_bakery", storage_available="ambient", capacity_remaining=100)
        # Anything passed in overrides the defaults.
        base.update(kw)
        return actions.create_org(db, **base)
    # Hand all of this to the test as one bundle.
    return dict(db=db, make_session=make_session, rules=rules, donor=donor, make_recipient=make_recipient)


def publish(world, quantity=20, minutes_left=120):
    # Posts a listing from the test donor: packed an hour ago, deadline "minutes_left" from now.
    return actions.publish_donation(world["db"], world["rules"], world["donor"].id, "packaged_bakery", "ambient", quantity,
                                    NOW - timedelta(minutes=60), NOW + timedelta(minutes=minutes_left), NOW)


def test_safe_window_takes_the_earliest_limit():
    rules = load_rules()
    prepared = NOW - timedelta(minutes=60)
    # If the donor's deadline (30 min) is sooner than the rule's limit, the donor's deadline wins.
    assert latest_collect(rules, "packaged_bakery", "ambient", prepared, NOW + timedelta(minutes=30)) == NOW + timedelta(minutes=30)
    # If the donor's deadline is far away (a day), the rule's limit wins (prepared + 480 minutes).
    assert latest_collect(rules, "packaged_bakery", "ambient", prepared, NOW + timedelta(days=1)) == prepared + timedelta(minutes=480)


def test_unreviewed_category_is_refused():
    # Fresh produce has no reviewed rule, so asking for a deadline must raise an error rather than guess.
    with pytest.raises(RuleError):
        latest_collect(load_rules(), "fresh_produce", "ambient", NOW, NOW + timedelta(hours=1))


def test_appendix_b_worked_example():
    # Two sets of factor scores from the design document's worked example.
    a = dict(time_margin=90, storage_fit=100, load_fit=80, fair_share=40, reliability=85)
    b = dict(time_margin=60, storage_fit=100, load_fit=100, fair_share=90, reliability=50)
    # Their combined scores must be exactly the numbers in the document.
    assert matching.combine(a) == 81.75 and matching.combine(b) == 79.0
    # A brand-new organisation (no history) starts at 50% reliability.
    assert matching.smoothed_reliability(0, 0) == 50


def test_fair_share_band_and_exclusion_reasons(env):
    world = env
    # A: close by, reliable, last got food 6 days ago. B: close by, brand new (never got food). C: has no storage for this food.
    near = world["make_recipient"]("A-near", 13.081, completed=8, attempts=8, last_received_at=NOW - timedelta(days=6))
    fresh = world["make_recipient"]("B-new", 13.085)
    world["make_recipient"]("C-no-cold-store", 13.082, storage_available="chilled")
    # Post a listing and see who matches.
    listing = publish(world)
    matches = actions.matches_for(world["db"], listing.id, NOW)
    # Only C is excluded, and the reason is the storage problem.
    assert [r.name for r in matches["excluded"]] == ["C-no-cold-store"]
    assert "STORAGE_REQUIREMENT_NOT_MET" in matches["excluded"][0].excluded_reasons
    # A and B are ranked, and fair-share rotation was applied.
    assert len(matches["ranked"]) == 2 and matches["ranked"][0].fair_share_applied
    assert matches["ranked"][0].name == "B-new"   # never got food -> goes first inside the band


def test_expired_listing_cannot_be_claimed(env):
    world = env
    recipient = world["make_recipient"]("R", 13.081)
    # A listing with 60 minutes left.
    listing = publish(world, minutes_left=60)
    # Trying to claim it 2 hours later must be refused.
    with pytest.raises(actions.Conflict):
        actions.claim(world["db"], listing.id, recipient.id, now=NOW + timedelta(hours=2))


def test_too_far_to_arrive_in_window_is_excluded(env):
    world = env
    # A recipient about 150 km away.
    far = world["make_recipient"]("Far", 14.5)       # about 150 km away (way too far)
    listing = publish(world, minutes_left=60)
    # Claiming must be refused because they can't arrive inside the safe window.
    with pytest.raises(actions.Conflict, match="CANNOT_ARRIVE"):
        actions.claim(world["db"], listing.id, far.id, NOW)


def test_concurrent_claims_have_exactly_one_winner(env):
    world = env
    # Eight recipients, all eligible.
    recs = [world["make_recipient"](f"R{i}", 13.081 + i * 0.001) for i in range(8)]
    listing = publish(world)
    # results collects "won"/"lost"; the barrier makes all threads wait until everyone is ready, then go together.
    results, barrier = [], threading.Barrier(len(recs))

    def go(rid):
        # Each thread uses its own database session, like a real separate visitor.
        s = world["make_session"]()
        # Wait for everybody, then claim at the same moment.
        barrier.wait()
        try:
            actions.claim(s, listing.id, rid, NOW)
            results.append("won")
        except actions.Conflict:
            results.append("lost")
        finally:
            s.close()

    # One thread per recipient: start them all, then wait for all of them to finish.
    ts = [threading.Thread(target=go, args=(r.id,)) for r in recs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    # Exactly one winner and seven losers, and the log must still be intact.
    assert results.count("won") == 1 and results.count("lost") == 7
    assert verify_chain(world["db"])["ok"]


def test_expiry_releases_and_republishes_only_if_window_open(env):
    world = env
    recipient = world["make_recipient"]("R", 13.081)
    listing = publish(world, minutes_left=120)
    claim = actions.claim(world["db"], listing.id, recipient.id, NOW)
    # 16 minutes later (claims last 15), one claim should expire.
    assert actions.expire_due_claims(world["db"], NOW + timedelta(minutes=16))["claims_expired"] == 1
    # Reload the listing from the database: it should be back on the board (still within its safe window).
    world["db"].refresh(listing)
    assert listing.status == "published"


def test_handover_refusal_needs_reason_and_is_not_penalised(env):
    world = env
    recipient = world["make_recipient"]("R", 13.081)
    listing = publish(world)
    claim = actions.claim(world["db"], listing.id, recipient.id, NOW)
    # Refusing without giving a reason must fail.
    with pytest.raises(actions.Conflict):
        actions.complete_handover(world["db"], claim.id, accepted=False)
    # Refusing with a reason works.
    actions.complete_handover(world["db"], claim.id, accepted=False, reason="packaging damaged")
    world["db"].refresh(recipient)
    assert recipient.completed == 0 and recipient.attempts == 1   # the attempt counts, but there's no extra penalty field


def test_impact_reconciles_and_tampering_is_detected(env):
    world = env
    recipient = world["make_recipient"]("R", 13.081)
    # A full successful rescue: 20 listed, 18 accepted.
    listing = publish(world, quantity=20)
    claim = actions.claim(world["db"], listing.id, recipient.id, NOW)
    actions.verify_pickup(world["db"], world["donor"].id, actions.pickup_code(claim.id), NOW)
    actions.complete_handover(world["db"], claim.id, accepted=True, accepted_quantity=18, now=NOW)
    # The impact numbers must match exactly what happened.
    assert actions.impact(world["db"]) == {"listed_units": 20, "collected_units": 18, "uncollected_units": 0,
                                      "handovers_completed": 1, "handovers_refused": 0, "incidents_reported": 0}
    # The log is currently intact.
    assert verify_chain(world["db"])["ok"]
    # Now sneakily change "18" to "180" directly in the database, as a tamperer would.
    world["db"].execute(text("UPDATE audit_events SET payload = replace(payload, '18', '180') WHERE type='HANDOVER_COMPLETED'"))
    world["db"].commit()
    # The chain check must now fail and point to the broken event.
    v = verify_chain(world["db"])
    assert v["ok"] is False and v["broken_at_event_id"] is not None


@pytest.fixture()
def api(tmp_path):
    # Imported here so the whole app only loads for tests that need it.
    import app.main as main_module
    from app import security
    from fastapi.testclient import TestClient
    # A fresh, empty test database just for this test.
    db_engine = make_engine(f"sqlite:///{tmp_path/'api.db'}")
    init_db(db_engine)
    make_session = sessionmaker(bind=db_engine, expire_on_commit=False)
    def override():
        # Replacement for the app's normal database session, pointing at the test database instead.
        with make_session() as db:
            yield db
    main_module.app.dependency_overrides[main_module.get_session] = override
    # Forget any failed logins left over from earlier tests.
    security._FAILED_LOGINS.clear()
    db = make_session()
    # Create the admin account the tests log in with.
    actions.create_org(db, name="Admin", role="admin", lat=0, lon=0, verified=True,
                       username="admin", password_hash=security.hash_password("admin-pass-1"))
    # A pretend browser that talks to the app directly.
    client = TestClient(main_module.app)

    def login(user, pw):
        # Log in and give back the ready-to-use header with the token.
        response = client.post("/api/auth/login", json=dict(username=user, password=pw))
        assert response.status_code == 200, response.text
        return {"Authorization": "Bearer " + response.json()["token"]}

    def register(user, role, **kw):
        # Register a new organisation with sensible defaults (extras override them).
        body = dict(name=user.title() + " Org", role=role, username=user, password="password-123", lat=13.08, lon=80.27)
        body.update(kw)
        response = client.post("/api/register", json=body)
        assert response.status_code == 200, response.text
        return response.json()["id"]
    # Give the test the client and the two helpers; when the test finishes, the code after "yield" cleans up.
    yield client, login, register
    main_module.app.dependency_overrides.clear()


# Standard recipient details used when registering test recipients.
REC_KW = dict(accepts_categories=["packaged_bakery"], storage_available=["ambient"], capacity_remaining=50)


def listing_body():
    # A ready-made listing request: 10 packs of bread packed 30 minutes ago, collect within 2 hours.
    now = utcnow()
    return dict(category="packaged_bakery", storage="ambient", quantity=10,
                prepared_at=(now - timedelta(minutes=30)).isoformat() + "Z",
                donor_deadline=(now + timedelta(hours=2)).isoformat() + "Z")


def test_login_required_everywhere(api):
    client, *_ = api
    # Every one of these pages must refuse anyone who isn't logged in.
    for path in ("/api/me", "/api/donations", "/api/feed", "/api/claims", "/api/impact", "/api/audit/verify", "/api/orgs"):
        assert client.get(path).status_code == 401, path


def test_full_flow_with_admin_approval(api):
    client, login, register = api
    # Register a donor and a recipient.
    register("bakery", "donor")
    recipient_id = register("shelter", "recipient", lat=13.081, **REC_KW)
    donor_headers, recipient_headers = login("bakery", "password-123"), login("shelter", "password-123")
    assert client.post("/api/donations", json=listing_body(), headers=donor_headers).status_code == 409   # not verified yet
    # The admin approves everyone.
    admin_headers = login("admin", "admin-pass-1")
    for org in client.get("/api/orgs", headers=admin_headers).json():
        if org["role"] != "admin":
            assert client.post(f"/api/orgs/{org['id']}/verify", headers=admin_headers).status_code == 200
    # Now publishing works.
    response = client.post("/api/donations", json=listing_body(), headers=donor_headers)
    assert response.status_code == 200, response.text
    donation_id = response.json()["id"]
    # The match list ranks the shelter first, and the explanation includes the safety disclaimer.
    m = client.get(f"/api/donations/{donation_id}/matches", headers=donor_headers).json()
    assert m["ranked"][0]["org_id"] == recipient_id and "not a certification" in m["ranked"][0]["explanation"]
    # The recipient sees exactly one item in its feed.
    assert len(client.get("/api/feed", headers=recipient_headers).json()["items"]) == 1
    # The recipient claims it; claiming twice must fail.
    claim_id = client.post(f"/api/donations/{donation_id}/claim", headers=recipient_headers).json()["claim_id"]
    assert client.post(f"/api/donations/{donation_id}/claim", headers=recipient_headers).status_code == 409
    assert client.post(f"/api/claims/{claim_id}/handover", json=dict(accepted=True, accepted_quantity=10), headers=recipient_headers).status_code == 409   # donor hasn't verified the code yet
    # Fetch the pickup code, and let the donor verify it.
    code = client.get("/api/claims", headers=recipient_headers).json()["items"][0]["pickup_code"]
    assert client.post("/api/pickups/verify", json=dict(code=code), headers=donor_headers).status_code == 200
    # Now the handover can be accepted and the claim is completed.
    r = client.post(f"/api/claims/{claim_id}/handover", json=dict(accepted=True, accepted_quantity=10), headers=recipient_headers)
    assert r.json()["status"] == "completed"
    # The log is intact and the impact shows 10 units collected.
    assert client.get("/api/audit/verify", headers=recipient_headers).json()["ok"] is True
    assert client.get("/api/impact", headers=donor_headers).json()["collected_units"] == 10


def test_roles_and_ownership_enforced(api):
    client, login, register = api
    # Two donors and two recipients.
    register("bakery1", "donor")
    register("bakery2", "donor")
    register("shelter1", "recipient", **REC_KW)
    register("shelter2", "recipient", **REC_KW)
    # The admin approves everyone.
    admin_headers = login("admin", "admin-pass-1")
    for org in client.get("/api/orgs", headers=admin_headers).json():
        if org["role"] != "admin":
            client.post(f"/api/orgs/{org['id']}/verify", headers=admin_headers)
    # Log everyone in (shortened names: b = bakery, s = shelter).
    b1, b2, s1, second_recipient_headers = (login(u, "password-123") for u in ("bakery1", "bakery2", "shelter1", "shelter2"))
    # Bakery 1 posts a listing.
    donation_id = client.post("/api/donations", json=listing_body(), headers=b1).json()["id"]
    assert client.post("/api/donations", json=listing_body(), headers=s1).status_code == 403     # recipients can't publish
    assert client.get(f"/api/donations/{donation_id}/matches", headers=b2).status_code == 403            # that's not your listing
    assert client.get("/api/feed", headers=b1).status_code == 403                                # donors don't have a feed
    assert client.post("/api/orgs/1/verify", headers=b1).status_code == 403                      # admins only
    assert client.get("/api/orgs", headers=s1).status_code == 403
    # Shelter 1 claims; shelter 2 must not be able to hand over shelter 1's claim.
    claim_id = client.post(f"/api/donations/{donation_id}/claim", headers=s1).json()["claim_id"]
    response = client.post(f"/api/claims/{claim_id}/handover", json=dict(accepted=True, accepted_quantity=5), headers=second_recipient_headers)
    assert response.status_code == 403                                                              # that's not your claim


def test_register_validation(api):
    client, login, register = api
    register("bakery", "donor")
    # A valid-looking signup body that we tweak one thing at a time below.
    base = dict(name="X Org", role="donor", username="bakery", password="password-123", lat=1, lon=1)
    assert client.post("/api/register", json=base).status_code == 409                             # username already taken
    assert client.post("/api/register", json={**base, "username": "newone", "password": "short"}).status_code == 422
    assert client.post("/api/register", json={**base, "username": "newone", "role": "admin"}).status_code == 422   # you can't just sign yourself up as admin
    assert client.post("/api/register", json={**base, "username": "Bad Name!"}).status_code == 422


def test_login_throttle_and_token_tampering(api):
    client, login, register = api
    register("bakery", "donor")
    # Five wrong passwords in a row...
    for _ in range(5):
        assert client.post("/api/auth/login", json=dict(username="bakery", password="wrong-password")).status_code == 401
    # ...and now even the RIGHT password is refused for a while (429 = too many attempts).
    assert client.post("/api/auth/login", json=dict(username="bakery", password="password-123")).status_code == 429
    from app import security
    # Reset the lockout so we can continue the test.
    security._FAILED_LOGINS.clear()
    h = login("bakery", "password-123")
    assert client.get("/api/me", headers=h).status_code == 200
    # Change the last 3 characters of the token: the signature no longer matches, so it must be rejected.
    bad = {"Authorization": h["Authorization"][:-3] + "abc"}
    assert client.get("/api/me", headers=bad).status_code == 401
    # A made-up token must be rejected too.
    assert client.get("/api/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401


def test_intake_endpoint_is_donor_only(api):
    client, login, register = api
    register("bakery", "donor")
    register("shelter", "recipient", **REC_KW)
    # A normal Speak-to-List request.
    body = dict(text="20 packs of bread packed 30 minutes ago collect within 2 hours", tz_offset_minutes=-330)
    # No login: 401. Logged in as a recipient: 403.
    assert client.post("/api/intake/parse", json=body).status_code == 401
    assert client.post("/api/intake/parse", json=body, headers=login("shelter", "password-123")).status_code == 403
    # As a donor it works, and it finds 20 packs of bakery food.
    response = client.post("/api/intake/parse", json=body, headers=login("bakery", "password-123"))
    assert response.status_code == 200 and response.json()["quantity"] == 20 and response.json()["category"] == "packaged_bakery"
    # A sentence over 500 characters is refused.
    assert client.post("/api/intake/parse", json=dict(text="x" * 501), headers=login("bakery", "password-123")).status_code == 422


def test_demo_seed_is_idempotent_and_generates_admin_password(tmp_path):
    from app import security
    from app.demo_data import seed_demo
    from sqlalchemy import select
    from app.tables import Organisation
    # A fresh database and session.
    eng = make_engine(f"sqlite:///{tmp_path/'seed.db'}")
    init_db(eng)
    s = sessionmaker(bind=eng, expire_on_commit=False)()
    # Seed without giving an admin password: one must be generated, and it must be long enough.
    seeded = seed_demo(s)
    assert seeded["admin_generated"] and len(seeded["admin_password"]) >= 12
    assert seed_demo(s) is None                                   # never overwrites data that's already there
    # The generated password really works for the admin account.
    admin = s.scalars(select(Organisation).where(Organisation.username == "admin")).one()
    assert security.check_password(seeded["admin_password"], admin.password_hash)
    assert not security.check_password("demo-admin-123", admin.password_hash)   # the public default doesn't get used when hosted


def test_health_and_security_headers(api):
    client, login, register = api
    # The health check answers "ok".
    response = client.get("/healthz")
    assert response.status_code == 200 and response.json() == {"ok": True}
    # The safety headers are present on every reply.
    assert response.headers["x-content-type-options"] == "nosniff" and response.headers["x-frame-options"] == "DENY"
    # API replies must never be cached.
    assert client.get("/api/rules").headers["cache-control"] == "no-store"


# ---------------- v0.6: passing offers along, pickup codes, incidents, fairness ----------------
def test_offer_goes_to_best_first_then_cascades_then_opens(env, monkeypatch):
    # Turn first-dibs on with a 5 minute window.
    monkeypatch.setenv("FOODBRIDGE_OFFER_WINDOW_MIN", "5")
    world = env
    # Three eligible recipients.
    recs = [world["make_recipient"](f"R{i}", 13.081 + i * 0.001) for i in range(3)]
    listing = publish(world, minutes_left=240)
    # One of them has first dibs.
    first = listing.offered_to
    assert first in {r.id for r in recs}
    # Another recipient tries to claim during that window and must be told to wait.
    other = next(r for r in recs if r.id != first)
    with pytest.raises(actions.Conflict, match="offered to another"):
        actions.claim(world["db"], listing.id, other.id, NOW)
    # Track who has had a turn so far.
    seen = [first]
    # After 6 and 12 minutes, the offer should move on to a new recipient each time.
    for k in (1, 2):
        assert actions.expire_due_claims(world["db"], NOW + timedelta(minutes=6 * k))["offers_advanced"] == 1
        world["db"].refresh(listing)
        assert listing.offered_to not in seen
        seen.append(listing.offered_to)
    # After all three have had a turn, the listing opens to everyone.
    actions.expire_due_claims(world["db"], NOW + timedelta(minutes=19))
    world["db"].refresh(listing)
    assert listing.offered_to is None                                  # everyone has had first dibs, so it's open to all
    # Now anyone can claim it.
    assert actions.claim(world["db"], listing.id, recs[0].id, NOW + timedelta(minutes=19)).status == "reserved"
    assert verify_chain(world["db"])["ok"]


def test_expired_claim_cascades_and_blocks_reclaim(env, monkeypatch):
    monkeypatch.setenv("FOODBRIDGE_OFFER_WINDOW_MIN", "5")
    world = env
    # Two eligible recipients (created just for their side effect, so we don't need to keep them).
    [world["make_recipient"](f"R{i}", 13.081 + i * 0.001) for i in range(2)]
    listing = publish(world, minutes_left=240)
    # The first-dibs holder claims it...
    first = listing.offered_to
    actions.claim(world["db"], listing.id, first, NOW)
    # ...but never shows up, so 16 minutes later their claim expires.
    assert actions.expire_due_claims(world["db"], NOW + timedelta(minutes=16))["claims_expired"] == 1
    world["db"].refresh(listing)
    assert listing.status == "published" and listing.offered_to not in (None, first)       # the next best one gets first dibs
    # After that offer also runs out, the listing opens to everyone.
    actions.expire_due_claims(world["db"], NOW + timedelta(minutes=30))
    world["db"].refresh(listing)
    assert listing.offered_to is None
    # But the no-show can't claim it again.
    with pytest.raises(actions.Conflict, match="earlier claim"):
        actions.claim(world["db"], listing.id, first, NOW + timedelta(minutes=30))        # a no-show stays blocked from that listing


def test_donation_expires_when_window_closes_and_counts_as_uncollected(env):
    world = env
    world["make_recipient"]("R", 13.081)
    # 20 units with 60 minutes left that nobody claims.
    listing = publish(world, quantity=20, minutes_left=60)
    # One minute after the window closes, the listing expires.
    assert actions.expire_due_claims(world["db"], NOW + timedelta(minutes=61))["donations_expired"] == 1
    world["db"].refresh(listing)
    # It's marked expired and its 20 units count as "not collected".
    assert listing.status == "expired" and actions.impact(world["db"])["uncollected_units"] == 20
    assert verify_chain(world["db"])["ok"]


def test_pickup_code_is_one_time_donor_bound_and_required_for_acceptance(env):
    world = env
    recipient = world["make_recipient"]("R", 13.081)
    # A second, unrelated donor.
    other_donor = actions.create_org(world["db"], name="Other", role="donor", lat=13.08, lon=80.27, verified=True)
    listing = publish(world)
    claim = actions.claim(world["db"], listing.id, recipient.id, NOW)
    code = actions.pickup_code(claim.id)
    # Accepting before the code is verified must fail.
    with pytest.raises(actions.Conflict, match="pickup not verified"):
        actions.complete_handover(world["db"], claim.id, accepted=True, accepted_quantity=5, now=NOW)
    # Empty, wrong, one-character-off and nonsense codes must all give the same "invalid code" error.
    for bad in ("", "FB-1-000000000000", code[:-1] + ("0" if code[-1] != "0" else "1"), "nonsense"):
        with pytest.raises(actions.Conflict, match="invalid code"):
            actions.verify_pickup(world["db"], world["donor"].id, bad, NOW)
    with pytest.raises(actions.Conflict, match="invalid code"):
        actions.verify_pickup(world["db"], other_donor.id, code, NOW)                # a different donor can't use it
    # The right donor can verify (even typing the code in capitals).
    assert actions.verify_pickup(world["db"], world["donor"].id, code.upper(), NOW)["recipient"] == "R"
    # Using it a second time must fail.
    with pytest.raises(actions.Conflict, match="already used"):
        actions.verify_pickup(world["db"], world["donor"].id, code, NOW)
    # Now the handover can go through.
    assert actions.complete_handover(world["db"], claim.id, accepted=True, accepted_quantity=5, now=NOW).status == "completed"


def test_incident_freezes_listing_and_admin_resolves(env):
    world = env
    recipient = world["make_recipient"]("R", 13.081)
    stranger = world["make_recipient"]("Stranger", 13.082)
    listing = publish(world)
    claim = actions.claim(world["db"], listing.id, recipient.id, NOW)
    # Someone not involved in the listing can't report a problem on it.
    with pytest.raises(actions.Forbidden):
        actions.report_incident(world["db"], stranger, listing.id, "looks bad", NOW)
    # The claimant can, and it freezes both the listing and the claim.
    inc = actions.report_incident(world["db"], recipient, listing.id, "smells off", NOW)
    world["db"].refresh(listing)
    world["db"].refresh(claim)
    assert listing.status == "frozen" and claim.status == "frozen"
    with pytest.raises(actions.Conflict):
        actions.complete_handover(world["db"], claim.id, accepted=False, reason="x", now=NOW)          # that claim isn't active anymore
    # Nobody can claim a frozen listing.
    with pytest.raises(actions.Conflict):
        actions.claim(world["db"], listing.id, stranger.id, NOW)
    # The admin discards the food: the listing is discarded and the claim closed.
    actions.resolve_incident(world["db"], inc.id, "discard", "binned", NOW)
    world["db"].refresh(listing)
    world["db"].refresh(claim)
    assert listing.status == "discarded" and claim.status == "closed"
    # The 20 units now count as uncollected, and one incident was reported.
    imp = actions.impact(world["db"])
    assert imp["uncollected_units"] == 20 and imp["incidents_reported"] == 1
    # An incident can only be resolved once.
    with pytest.raises(actions.Conflict, match="already resolved"):
        actions.resolve_incident(world["db"], inc.id, "release", "", NOW)
    # Second scenario: the donor reports a torn package, and the admin decides it's fine to release.
    d2 = publish(world)
    c2 = actions.claim(world["db"], d2.id, recipient.id, NOW)
    inc2 = actions.report_incident(world["db"], world["donor"], d2.id, "packaging torn", NOW)
    actions.resolve_incident(world["db"], inc2.id, "release", "checked ok", NOW)
    world["db"].refresh(d2)
    world["db"].refresh(c2)
    # The listing goes back on the board and the old claim is expired.
    assert d2.status == "published" and c2.status == "expired"
    assert verify_chain(world["db"])["ok"]


def test_fairness_view_counts_offers_and_collected_share(env, monkeypatch):
    monkeypatch.setenv("FOODBRIDGE_OFFER_WINDOW_MIN", "5")
    world = env
    # Three recipients named A, B and C.
    [world["make_recipient"](n, 13.081 + i * 0.001) for i, n in enumerate("ABC")]
    listing = publish(world, quantity=10, minutes_left=240)
    # The first-dibs holder completes a full rescue of 10 units.
    first = listing.offered_to
    cl = actions.claim(world["db"], listing.id, first, NOW)
    actions.verify_pickup(world["db"], world["donor"].id, actions.pickup_code(cl.id), NOW)
    actions.complete_handover(world["db"], cl.id, accepted=True, accepted_quantity=10, now=NOW)
    f = actions.fairness(world["db"])
    # That recipient's row shows 1 offer, 1 completion, 10 units, and 100% of everything collected.
    row = next(x for x in f["rows"] if x["org_id"] == first)
    assert row["offers"] == 1 and row["completed"] == 1 and row["collected_units"] == 10 and row["share_pct"] == 100
    # Because one organisation got everything, the concentration warning is on.
    assert f["total_collected"] == 10 and f["concentration_warning"] is True


def test_api_pickup_incident_fairness_flow(api):
    client, login, register = api
    register("bakery", "donor")
    register("shelter", "recipient", **REC_KW)
    # The admin approves everyone.
    admin_headers = login("admin", "admin-pass-1")
    for org in client.get("/api/orgs", headers=admin_headers).json():
        if org["role"] != "admin":
            client.post(f"/api/orgs/{org['id']}/verify", headers=admin_headers)
    donor_headers, recipient_headers = login("bakery", "password-123"), login("shelter", "password-123")
    # Post, claim and look at the claim's pickup details.
    donation_id = client.post("/api/donations", json=listing_body(), headers=donor_headers).json()["id"]
    claim_id = client.post(f"/api/donations/{donation_id}/claim", headers=recipient_headers).json()["claim_id"]
    item = client.get("/api/claims", headers=recipient_headers).json()["items"][0]
    # The claim must show a code starting "FB-", a QR picture, and "not picked up yet".
    assert item["pickup_code"].startswith("FB-") and item["qr_svg"].startswith("<svg") and item["picked_up"] is False
    assert client.post("/api/pickups/verify", json=dict(code=item["pickup_code"]), headers=recipient_headers).status_code == 403    # only donors can do this
    assert client.post("/api/pickups/verify", json=dict(code=item["pickup_code"]), headers=donor_headers).status_code == 200
    assert client.post("/api/pickups/verify", json=dict(code=item["pickup_code"]), headers=donor_headers).status_code == 409  # works once only
    # Finish the handover.
    assert client.post(f"/api/claims/{claim_id}/handover", json=dict(accepted=True, accepted_quantity=10), headers=recipient_headers).status_code == 200
    # Report a problem after collection.
    response = client.post(f"/api/donations/{donation_id}/incident", json=dict(note="felt unwell after eating"), headers=recipient_headers)
    assert response.status_code == 200 and response.json()["donation_status"] == "collected"    # gets recorded, but a closed listing stays closed
    # The admin sees one open incident; a recipient can't see the incidents list.
    inc = client.get("/api/incidents", headers=admin_headers).json()
    assert len(inc) == 1 and inc[0]["status"] == "open"
    assert client.get("/api/incidents", headers=recipient_headers).status_code == 403
    # The admin resolves it.
    assert client.post(f"/api/incidents/{inc[0]['id']}/resolve", json=dict(decision="release", note="reviewed"), headers=admin_headers).status_code == 200
    # Fairness is admin-only and shows 10 units collected.
    assert client.get("/api/fairness", headers=admin_headers).json()["total_collected"] == 10
    assert client.get("/api/fairness", headers=recipient_headers).status_code == 403
    # Impact numbers include the incident and the collected units.
    imp = client.get("/api/impact", headers=donor_headers).json()
    assert imp["incidents_reported"] == 1 and imp["collected_units"] == 10
    # The log is still intact, and the manual expire sweep has nothing to do.
    assert client.get("/api/audit/verify", headers=admin_headers).json()["ok"] is True
    assert client.post("/api/maintenance/expire", headers=admin_headers).json()["claims_expired"] == 0


def test_tamil_has_a_translation_for_every_english_string():
    """The website must never fall back to English in Tamil mode. We compare the keys of the two dictionaries."""
    import re
    from pathlib import Path
    # Read the website's single HTML file.
    html = (Path(__file__).resolve().parent.parent / "web" / "index.html").read_text(encoding="utf-8")
    # Cut out the block of translations.
    block = html[html.index("const I18N"):html.index("const t = k")]
    # Split it into the English half and the Tamil half.
    en, ta = block[block.index(" en: {"):block.index(" ta: {")], block[block.index(" ta: {"):]
    # A little tool that pulls out every translation key name from a piece of text.
    keys = lambda s: set(re.findall(r'''(?:^|[,{]\s*)["']?([A-Za-z_][A-Za-z0-9_]*)["']?\s*:\s*["']''', s))
    # Any English key that has no Tamil twin is a missing translation.
    missing = sorted(keys(en) - keys(ta))
    assert not missing, f"no Tamil text for: {missing}"
    # Sanity check that we actually found a lot of Tamil keys (so the check isn't passing by accident).
    assert len(keys(ta)) > 240


def test_public_stats_is_anonymous_and_counts_only(api):
    """The landing page shows live totals before login: just counts, no names or listings."""
    client, login, register = api
    response = client.get("/api/public/stats")          # no login header (so not logged in)
    assert response.status_code == 200
    # Only four numbers are exposed, nothing else.
    assert set(response.json()) == {"collected_units", "handovers_completed", "donors", "recipients"}
    register("newdonor", "donor")           # signed up, but an admin hasn't approved them yet
    # Unapproved organisations aren't counted.
    assert client.get("/api/public/stats").json()["donors"] == 0
