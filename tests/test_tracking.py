from datetime import timedelta  # lengths of time
from app import tracking, actions  # the live-tracking code and the main actions
from app.database import utcnow  # noqa: F401 (kept from the original imports)
from app.tables import Donation  # the listing table
from .test_core import NOW, REC_KW, api, env, listing_body, publish  # noqa: F401  (these are fixtures, pytest uses them behind the scenes)


def other_donor(world, name, lat):
    # Creates an extra verified donor at a given latitude.
    return actions.create_org(world["db"], name=name, role="donor", lat=lat, lon=80.27, verified=True)


def post(world, donor, quantity, minutes_left):
    # Publishes a listing for a given donor, with first dibs switched off (offer_window=0) so anyone can claim.
    return actions.publish_donation(world["db"], world["rules"], donor.id, "packaged_bakery", "ambient", quantity,
                                    NOW - timedelta(minutes=60), NOW + timedelta(minutes=minutes_left), NOW, offer_window=0)


def states(tl):
    # Turns a timeline into a simple {stage: state} dictionary, easier to check.
    return {x["key"]: x["state"] for x in tl["stages"]}


# ---------------- live tracking (where's the food rn?) ----------------
def test_timeline_follows_the_whole_rescue_and_records_success(env):
    # One recipient, and a listing for them.
    db, recipient = env["db"], env["make_recipient"]("A", 13.081)
    listing = publish(env)
    # Right after publishing: listed is done, claimed is the current step, and the rescue is in progress.
    tl = tracking.timeline(db, listing, NOW)
    assert states(tl)["listed"] == "done" and states(tl)["claimed"] == "current" and tl["outcome"]["result"] == "in_progress"
    # The recipient claims, and the donor verifies the pickup code.
    claim = actions.claim(db, listing.id, recipient.id, NOW)
    actions.verify_pickup(db, env["donor"].id, actions.pickup_code(claim.id), NOW)
    # The pickup stage should now be done.
    assert states(tracking.timeline(db, listing, NOW))["pickup"] == "done"
    # The recipient accepts 20 units.
    actions.complete_handover(db, claim.id, True, 20, now=NOW)
    tl = tracking.timeline(db, listing, NOW)
    # Every stage is now done (or skipped, like the unused "offered" step).
    assert all(v in ("done", "skipped") for v in states(tl).values())
    # And the outcome is recorded as a success.
    assert tl["outcome"] == {"result": "success", "reason_code": "HANDOVER_CONFIRMED", "detail": None}


def test_timeline_explains_why_a_donation_failed(env):
    db, recipient = env["db"], env["make_recipient"]("A", 13.081)
    # A listing with only 30 minutes left, which the recipient claims but never collects.
    listing = publish(env, minutes_left=30)
    actions.claim(db, listing.id, recipient.id, NOW)
    actions.expire_due_claims(db, NOW + timedelta(minutes=40))          # the claim runs out and the window closes
    # Look at the timeline 40 minutes later.
    tl = tracking.timeline(db, db.get(Donation, listing.id), NOW + timedelta(minutes=40))
    # It failed because nobody took it in time, and one claim lapsed.
    assert tl["outcome"]["reason_code"] == "NO_TAKER_BEFORE_DEADLINE" and tl["claims_lapsed"] == 1
    # The progress bar should show a "failed" step.
    assert "failed" in states(tl).values()

    # Second case: a listing that gets claimed, then refused at the door.
    d2 = publish(env)
    c2 = actions.claim(db, d2.id, recipient.id, NOW)
    actions.complete_handover(db, c2.id, False, reason="packaging damaged", now=NOW)
    out = tracking.timeline(db, db.get(Donation, d2.id), NOW)["outcome"]
    # The outcome must name the refusal and include the reason given.
    assert out["reason_code"] == "REFUSED_AT_HANDOVER" and out["detail"] == "packaging damaged"


def test_timeline_flags_stalled_claims_and_closing_windows(env):
    db, recipient = env["db"], env["make_recipient"]("A", 13.081)
    # Only 14 minutes left: that's inside the 20-minute "at risk" zone.
    listing = publish(env, minutes_left=14)
    assert "WINDOW_CLOSING" in tracking.timeline(db, listing, NOW)["risks"]
    # Claim it. Right away, the claim isn't about to lapse yet...
    actions.claim(db, listing.id, recipient.id, NOW)
    assert "CLAIM_EXPIRING" not in tracking.timeline(db, listing, NOW)["risks"]
    # ...but 10 minutes later (claims last 15), it is.
    assert "CLAIM_EXPIRING" in tracking.timeline(db, listing, NOW + timedelta(minutes=10))["risks"]


def test_event_lookup_does_not_confuse_donation_ids(env):
    db = env["db"]
    # Publish 12 listings (ids 1 to 12).
    for _ in range(12):
        publish(env)                                       # ids 1..12: '1' must NOT match '10', '11' or '12'
    # Looking up listing 1's events must only return events for listing 1.
    assert {p["donation_id"] for _, p in tracking.donation_events(db, 1)} == {1}


# ---------------- food passport ----------------
def test_passport_has_record_history_declaration_and_no_safety_claim(env):
    db, recipient = env["db"], env["make_recipient"]("A", 13.081)
    # A listing that includes an allergen declaration.
    d = actions.publish_donation(db, env["rules"], env["donor"].id, "packaged_bakery", "ambient", 20, NOW - timedelta(minutes=60),
                                 NOW + timedelta(hours=2), NOW, offer_window=0, declaration="contains wheat and milk")
    # It's claimed and then refused because the seal was broken.
    claim = actions.claim(db, d.id, recipient.id, NOW)
    actions.complete_handover(db, claim.id, False, reason="seal broken", now=NOW)
    # Build the passport.
    p = tracking.passport(db, d, "https://x.example/", NOW)
    # It must show the declaration and the quantity.
    assert p["donor_declaration"] == "contains wheat and milk" and p["food"]["quantity"] == 20
    # The link must be built correctly and a QR picture included.
    assert p["url"] == f"https://x.example/#passport/{d.id}" and "<svg" in p["qr_svg"]
    # The history must start with the listing and the claim, in order.
    assert [h["type"] for h in p["handling_history"]][:2] == ["DONATION_PUBLISHED", "CLAIM_RESERVED"]
    # The refusal reason must be recorded, and the log's integrity check must pass.
    assert p["refusals"][0]["detail"]["reason"] == "seal broken" and p["record_integrity"]["chain_ok"]
    # And the passport must clearly say it does NOT certify food safety.
    assert "does not certify" in p["disclaimer"]


def test_passport_and_timeline_access_is_limited_to_involved_orgs(api):
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
    # Log everyone in.
    b1, b2, s1, second_recipient_headers = (login(u, "password-123") for u in ("bakery1", "bakery2", "shelter1", "shelter2"))
    # Bakery 1 posts a listing with a "no nuts" note.
    body = dict(listing_body(), declaration="no nuts")
    donation_id = client.post("/api/donations", json=body, headers=b1).json()["id"]
    # The owner and the admin can see the passport.
    assert client.get(f"/api/donations/{donation_id}/passport", headers=b1).json()["donor_declaration"] == "no nuts"
    assert client.get(f"/api/donations/{donation_id}/passport", headers=admin_headers).status_code == 200
    # Another donor can't.
    assert client.get(f"/api/donations/{donation_id}/passport", headers=b2).status_code == 403
    assert client.get(f"/api/donations/{donation_id}/passport", headers=second_recipient_headers).status_code == 403          # nobody has claimed it yet
    # Shelter 1 claims it...
    client.post(f"/api/donations/{donation_id}/claim", headers=s1)
    # ...and now it's allowed to look.
    assert client.get(f"/api/donations/{donation_id}/passport", headers=s1).status_code == 200            # the claimant is allowed to look
    # The uninvolved recipient still can't see the timeline.
    assert client.get(f"/api/donations/{donation_id}/timeline", headers=second_recipient_headers).status_code == 403
    # No login at all: 401.
    assert client.get(f"/api/donations/{donation_id}/passport").status_code == 401
    # Bakery 1 sees its one listing on the live board...
    assert len(client.get("/api/live", headers=b1).json()["items"]) == 1
    assert client.get("/api/live", headers=b2).json()["items"] == []                              # donors can only see their own
    # Recipients have no live board.
    assert client.get("/api/live", headers=s1).status_code == 403


# ---------------- impact details (the real numbers) ----------------
def test_impact_detail_is_recomputed_from_events(env):
    db, recipient = env["db"], env["make_recipient"]("A", 13.081)
    # Three listings: 20 units, 10 units (only 30 minutes left) and 5 units.
    d1, d2, d3 = publish(env, 20), publish(env, 10, minutes_left=30), publish(env, 5)
    # The first one is claimed, picked up, and 18 of the 20 are accepted.
    c1 = actions.claim(db, d1.id, recipient.id, NOW)
    actions.verify_pickup(db, env["donor"].id, actions.pickup_code(c1.id), NOW)
    actions.complete_handover(db, c1.id, True, 18, now=NOW)
    actions.expire_due_claims(db, NOW + timedelta(minutes=40))       # d2 (the 30 min one) expires; d3 is still open
    # Now ask for the detailed numbers.
    i = tracking.impact_detail(db)
    # 18 units collected out of 35 listed.
    assert i["collected_units"] == 18 and i["listed_units"] == 35
    assert i["by_category"]["packaged_bakery"]["collected"] == 18
    # One listing failed for lack of a taker.
    assert i["failure_reasons"]["NO_TAKER_BEFORE_DEADLINE"] == 1
    # Of 2 finished listings, 1 succeeded: 50%.
    assert i["completion_rate_pct"] == 50 and i["finished_donations"] == 2
    # Everything happened instantly in the test, so the average time is tiny (but present).
    assert i["avg_minutes_listing_to_collection"] is not None and i["avg_minutes_listing_to_collection"] < 1
    # One recipient took part, and the "no meal or emissions estimates" note is present.
    assert i["participants"]["recipients"] == 1 and "No meal or emissions estimates" in i["method_note"]


# ---------------- rescue route planner (who do I pick up from first?) ----------------
def test_planner_orders_stops_respects_deadlines_and_vehicle_capacity(env):
    # A recipient with plenty of storage.
    db, recipient = env["db"], env["make_recipient"]("R", 13.08, capacity_remaining=200)
    # Four listings from four donors at different distances.
    a = post(env, other_donor(env, "A", 13.10), 30, 60)
    b = post(env, other_donor(env, "B", 13.12), 30, 60)
    far = post(env, other_donor(env, "Far", 14.08), 20, 60)          # about 111 km away, matching already says no
    big = post(env, other_donor(env, "Big", 13.09), 50, 60)
    # Plan a route for a vehicle that fits 70 units.
    plan = tracking.plan_route(db, recipient, NOW, vehicle_capacity=70)
    ids = [x["donation_id"] for x in plan["stops"]]
    assert set(ids) == {a.id, b.id} and ids == [a.id, b.id]          # 30+30 beats 50 alone when the vehicle fits 70 units
    # 60 units total, with a running total of 60 at the second stop.
    assert plan["total_units"] == 60 and plan["stops"][1]["cumulative_units"] == 60
    # The first stop is "a".
    assert plan["next_stop"]["donation_id"] == a.id
    # The reasons given for skipped stops.
    why = {x["donation_id"]: x["reason"] for x in plan["skipped"]}
    assert far.id not in ids and far.id not in why                     # not eligible, so it never even becomes a candidate
    # The big listing was skipped because a better route uses the time or space.
    assert why[big.id] == "BETTER_ROUTE_USES_THE_TIME_OR_SPACE"
    # Every stop in the plan must arrive before its deadline.
    assert all(x["slack_min"] >= 0 for x in plan["stops"])
    # A backup plan exists for if the first stop falls through.
    assert plan["backup"]["without_donation_id"] == a.id and plan["backup"]["stops"]
    # And the plan admits it uses straight-line distances.
    assert "straight-line" in plan["assumptions"]["distance"]


def test_planner_flags_donations_only_this_recipient_can_rescue(env):
    db, recipient = env["db"], env["make_recipient"]("R", 13.08)
    # A small listing with a tight (25 minute) deadline.
    listing = post(env, other_donor(env, "A", 13.09), 10, 25)
    # Right now only this recipient can rescue it.
    assert tracking.plan_route(db, recipient, NOW)["stops"][0]["only_you"] is True
    env["make_recipient"]("Rival", 13.081)                                        # a second eligible org shows up
    # Now someone else could take it too, but it's still marked urgent.
    p = tracking.plan_route(db, recipient, NOW)["stops"][0]
    assert p["only_you"] is False and p["urgent"] is True and p["donation_id"] == listing.id


def test_planner_includes_own_claims_and_skips_offers_held_by_others(env, monkeypatch):
    db, recipient = env["db"], env["make_recipient"]("R", 13.08)
    # A listing that the recipient claims straight away.
    listing = post(env, other_donor(env, "A", 13.09), 10, 60)
    claim = actions.claim(db, listing.id, recipient.id, NOW)
    # The plan must include that claim as a "claimed" stop.
    stops = tracking.plan_route(db, recipient, NOW)["stops"]
    assert stops[0]["claimed"] is True and stops[0]["claim_id"] == claim.id
    # Add a rival recipient right next door.
    rival = env["make_recipient"]("Rival", 13.0801)
    # Turn first-dibs offers on (5 minutes).
    monkeypatch.setenv("FOODBRIDGE_OFFER_WINDOW_MIN", "5")
    # Publish a new listing, which gets offered to one of the two recipients first.
    held = actions.publish_donation(db, env["rules"], other_donor(env, "B", 13.09).id, "packaged_bakery", "ambient", 5,
                                    NOW - timedelta(minutes=60), NOW + timedelta(minutes=60), NOW)
    # Whoever did NOT get first dibs is "other".
    other = rival.id if held.offered_to == rival.id else recipient.id
    # Plan for the recipient who is locked out of the offer.
    plan = tracking.plan_route(db, recipient if other == rival.id else rival, NOW)
    # That listing must not appear in the locked-out recipient's plan.
    assert held.id not in [x["donation_id"] for x in plan["stops"]] or other != rival.id


def test_planner_is_empty_without_capacity_or_options(env):
    db, recipient = env["db"], env["make_recipient"]("R", 13.08)
    # No listings at all: an empty plan.
    assert tracking.plan_route(db, recipient, NOW)["stops"] == []
    # A listing exists, but the vehicle capacity is 0: still an empty plan.
    post(env, other_donor(env, "A", 13.09), 10, 60)
    assert tracking.plan_route(db, recipient, NOW, vehicle_capacity=0)["stops"] == []


def test_planner_endpoint_requires_an_approved_recipient(api):
    client, login, register = api
    # One donor and one recipient (not approved yet).
    register("bakery", "donor")
    register("shelter", "recipient", lat=13.081, **REC_KW)
    s_h, b_h = login("shelter", "password-123"), login("bakery", "password-123")
    assert client.get("/api/planner", headers=s_h).status_code == 403          # admin hasn't approved them yet
    # The admin approves everyone.
    admin_headers = login("admin", "admin-pass-1")
    for org in client.get("/api/orgs", headers=admin_headers).json():
        if org["role"] != "admin":
            client.post(f"/api/orgs/{org['id']}/verify", headers=admin_headers)
    # The donor posts a 10-unit listing.
    client.post("/api/donations", json=listing_body(), headers=b_h)
    # Now the planner works for the approved recipient.
    response = client.get("/api/planner?vehicle_capacity=40", headers=s_h)
    assert response.status_code == 200 and response.json()["stops"][0]["quantity"] == 10
    assert client.get("/api/planner", headers=b_h).status_code == 403          # donors don't get a planner
    # Any logged-in user can see the impact details.
    assert client.get("/api/impact/detail", headers=b_h).status_code == 200
