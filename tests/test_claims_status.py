# Re-use the shared fixtures and helpers from test_core.py (pytest uses the fixtures behind the scenes).
from .test_core import REC_KW, api, env, listing_body  # noqa: F401  (these are fixtures, pytest uses them behind the scenes)


def test_claims_list_reports_the_claims_own_status(api):
    """A completed claim has to say 'completed' (the listing's own status is 'collected'). It used to get overwritten, oops."""
    # "api" gives us a fake web client plus helpers to log in and register.
    client, login, register = api
    # Sign up one donor and one recipient.
    register("bakery", "donor")
    register("shelter", "recipient", **REC_KW)
    # The admin has to approve new organisations before they can do anything.
    admin_headers = login("admin", "admin-pass-1")
    for org in client.get("/api/orgs", headers=admin_headers).json():
        # Approve everyone except the admin account.
        if org["role"] != "admin":
            client.post(f"/api/orgs/{org['id']}/verify", headers=admin_headers)
    # Log both of them in.
    donor_headers, recipient_headers = login("bakery", "password-123"), login("shelter", "password-123")
    # The donor posts a listing...
    donation_id = client.post("/api/donations", json=listing_body(), headers=donor_headers).json()["id"]
    # ...and the recipient claims it.
    claim_id = client.post(f"/api/donations/{donation_id}/claim", headers=recipient_headers).json()["claim_id"]
    # Grab the pickup code from the recipient's claims list.
    code = client.get("/api/claims", headers=recipient_headers).json()["items"][0]["pickup_code"]
    # Before pickup, the claim should say "reserved".
    assert client.get("/api/claims", headers=recipient_headers).json()["items"][0]["status"] == "reserved"
    # The donor verifies the code...
    assert client.post("/api/pickups/verify", json={"code": code}, headers=donor_headers).status_code == 200
    # ...and the recipient accepts 10 units.
    assert client.post(f"/api/claims/{claim_id}/handover", json=dict(accepted=True, accepted_quantity=10), headers=recipient_headers).status_code == 200
    # Look at the claim again.
    item = client.get("/api/claims", headers=recipient_headers).json()["items"][0]
    # The CLAIM says completed, while the LISTING says collected (that was the bug).
    assert item["status"] == "completed" and item["donation_status"] == "collected"
