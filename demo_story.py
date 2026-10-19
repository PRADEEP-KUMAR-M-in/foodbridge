"""Runs one full rescue through the live API so judges can watch the Live tab update in real time.

    uvicorn app.main:app            # in one terminal
    python reset_demo.py            # fresh demo data (do this before starting the server)
    python demo_story.py            # in another terminal; open the Live tab as bakery or admin

Only uses fictional demo accounts. Set FB_URL to point at a hosted copy.
"""
import json  # to send and read JSON
import os  # to read settings
import time  # for the pauses between steps
import urllib.request  # a built-in way to make web requests (so no extra libraries are needed)
from datetime import datetime, timedelta, timezone  # date+time and lengths of time

# The server address (default: the one running on your own computer). The trailing slash is removed.
URL = os.getenv("FB_URL", "http://127.0.0.1:8000").rstrip("/")
# Seconds to pause between steps so a person can watch the screen update (default 4).
PAUSE = float(os.getenv("FB_PAUSE", "4"))


def call(path, token=None, body=None):
    # Build a request: POST if there's a body to send, otherwise GET. The body is turned into JSON bytes, and the login token (if any) goes in the header.
    request = urllib.request.Request(URL + "/api" + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
    try:
        # Send it and read the reply as JSON.
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        # If the server says no, stop the whole script with a clear message showing which call failed.
        raise SystemExit(f"{path} -> {error.code} {error.read().decode()}")


def login(username, password="demo-pass-123"):
    # Log in and give back just the token.
    return call("/auth/login", body={"username": username, "password": password})["token"]


def step(msg):
    # Print what's about to happen, then pause so people can follow along.
    print(f"\n>> {msg}", flush=True)
    time.sleep(PAUSE)


# The current time in UTC.
now = datetime.now(timezone.utc)
# Small helper: format a date+time the way the API expects (like 2026-10-17T13:30:00Z).
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")
# Log in as the bakery, and as both demo shelters.
bakery = login("bakery")
shelters = {username: login(username) for username in ("shelter-a", "shelter-b")}

step("Bakery lists 24 packs of packaged bakery (packed 20 min ago, collect within 90 min)")
# The bakery posts the listing; we keep the new listing's id.
donation_id = call("/donations", bakery, dict(category="packaged_bakery", storage="ambient", quantity=24, declaration="Contains wheat. Sealed packs.",
                                      prepared_at=iso(now - timedelta(minutes=20)), donor_deadline=iso(now + timedelta(minutes=90))))["id"]
# the best-ranked org gets first dibs; the other one only sees the listing once the offer window ends
# Find the shelter that is eligible AND isn't blocked by someone else's first-dibs offer.
chosen_shelter = next(username for username, token in shelters.items()
           if any(item["id"] == donation_id and item["eligible"] and item.get("offer") != "other" for item in call("/feed", token)["items"]))
shelter = shelters[chosen_shelter]
step(f"{chosen_shelter} has first dibs, sees the plan and claims the stop")
# Ask for the shelter's rescue plan and print its stops.
plan = call("/planner", shelter)
print("   planned stops:", [(stop["donor"], stop["quantity"]) for stop in plan["stops"]])
# The shelter claims the listing.
call(f"/donations/{donation_id}/claim", shelter, {})
step(f"{chosen_shelter} shows its pickup code; the bakery verifies it")
# Find the pickup code on the shelter's claim for this listing.
code = next(claim["pickup_code"] for claim in call("/claims", shelter)["items"] if claim["id"] == donation_id)
# The bakery confirms the code.
call("/pickups/verify", bakery, {"code": code})
step(f"{chosen_shelter} inspects the food and accepts 24 packs")
# Find the claim's id, then complete the handover.
claim_id = next(claim["claim_id"] for claim in call("/claims", shelter)["items"] if claim["id"] == donation_id)
call(f"/claims/{claim_id}/handover", shelter, {"accepted": True, "accepted_quantity": 24})
# Fetch the final timeline and print how it ended.
timeline = call(f"/donations/{donation_id}/timeline", bakery)
print("   outcome:", timeline["outcome"]["result"], "| stages:", [(stage["key"], stage["state"]) for stage in timeline["stages"]])
print("\nDone. Open the passport for donation", donation_id, "to see the full handling history.")
