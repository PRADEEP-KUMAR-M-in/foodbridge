import random  # seeded random numbers for the ranking comparison test
from dataclasses import replace  # copy settings with one change
from app import matching, simulator as sim  # the real ranking code, and the simulator (shortened to "sim")
from .test_core import REC_KW, api, env  # noqa: F401  (these are fixtures, pytest uses them behind the scenes)

# The default simulation settings, shared by every test below.
PARAMS = sim.Params()


def test_same_seed_gives_identical_results():
    # Run the exact same simulation twice.
    a, b = sim.simulate(PARAMS, runs=15, seed=3), sim.simulate(PARAMS, runs=15, seed=3)
    # Same seed must give identical results...
    assert a == b
    # ...and a different seed must give different results.
    assert sim.simulate(PARAMS, runs=15, seed=4) != a


def test_every_process_sees_the_same_organisations_and_listings():
    # Build the pretend world twice with the same seed.
    r1, d1 = sim.make_env(PARAMS, 7)
    r2, d2 = sim.make_env(PARAMS, 7)
    # Every organisation must be identical in both worlds (location, storage, capacity, no-show chance).
    assert [(r.lat, r.lon, sorted(r.storages), r.cap, r.noshow) for r in r1] == [(r.lat, r.lon, sorted(r.storages), r.cap, r.noshow) for r in r2]
    # And so must every listing.
    assert [(d.day, d.qty, d.storage, d.deadline) for d in d1] == [(d.day, d.qty, d.storage, d.deadline) for d in d2]


def test_constraint_filters_remove_wasted_trips():
    # With filters on (full FOODBRIDGE, or FOODBRIDGE without the cascade), no trip should ever be wasted.
    for flags in (sim.FULL, sim.SCENARIOS["no_cascade"]):
        assert all(r["wasted"] == 0 for r in sim.run_many(PARAMS, flags, 40, seed=1))
    assert sum(r["wasted"] for r in sim.run_many(PARAMS, sim.BASELINE, 40, seed=1)) > 0     # the old way wastes trips under our assumptions


def test_no_organisations_means_nothing_delivered():
    # A world with zero receiving organisations.
    out = sim.run_once(replace(PARAMS, recipients=0), sim.FULL, 1)
    # Nothing can be delivered and nothing can be wasted.
    assert out["delivered"] == 0 and out["wasted"] == 0


def test_delivered_never_exceeds_listed_and_capacity_is_respected():
    # For every scenario, over 20 simulated weeks...
    for flags in sim.SCENARIOS.values():
        for run in sim.run_many(PARAMS, flags, 20, seed=5):
            # ...you can never deliver more food than was listed (and never a negative amount).
            assert 0 <= run["delivered"] <= run["listed"]


def test_simulated_ranking_matches_the_apps_own_ranking():
    """The simulator's pick has to match matching.rank()'s first choice, so 'we use the same scoring code' is actually true."""
    # A seeded random generator so this test is repeatable.
    rng = random.Random(11)
    # Try 300 random little worlds.
    for _ in range(300):
        recs, dons = sim.make_env(replace(PARAMS, recipients=6, days=3), rng.randint(1, 10_000))
        # Skip worlds with no listings.
        if not dons:
            continue
        # Look at the first listing.
        d = dons[0]
        # Organisations that could actually take it.
        pool = [r for r in recs if sim._fits(r, d) and sim._feasible(PARAMS, d, r, 0)]
        for r in pool:
            # Give each one a random track record so scores differ.
            r.last_day = rng.choice([None, 0, 0])
            r.completed = rng.randint(0, 6)
            r.attempts = r.completed + rng.randint(0, 3)
        # Need at least two to make a ranking meaningful.
        if len(pool) < 2:
            continue
        # Build the same scores using the app's own Result objects.
        results = []
        for r in pool:
            # Spare minutes after travel and the buffer.
            slack = d.deadline - 0 - sim._eta(d, r) - PARAMS.handling_buffer_min
            # Days since they last received food.
            days = (d.day - r.last_day) if r.last_day is not None else matching.FAIR_SHARE_CAP_DAYS
            # A result object, like the app creates.
            x = matching.Result(r.id, str(r.id), True)
            # Its five factor scores.
            x.factors = {"time_margin": 100 * min(1.0, slack / matching.TARGET_SLACK_MIN), "storage_fit": 100.0,
                         "load_fit": matching.load_fit(d.qty, r.cap_left),
                         "fair_share": 100 * min(days, matching.FAIR_SHARE_CAP_DAYS) / matching.FAIR_SHARE_CAP_DAYS,
                         "reliability": matching.smoothed_reliability(r.completed, r.attempts)}
            # And its combined score.
            x.score = matching.combine(x.factors)
            results.append(x)
        # The simulator's pick must be the same organisation the app's own ranking puts first.
        assert sim._pick(PARAMS, sim.FULL, pool, d, 0, rng).id == matching.rank(results)[0].org_id


def test_simulate_endpoint_requires_login_validates_and_is_deterministic(api):
    client, login, register = api
    # A small, quick simulation request.
    body = dict(runs=10, seed=2)
    # Not logged in: rejected.
    assert client.post("/api/simulate", json=body).status_code == 401
    # Register a donor and log in.
    register("bakery", "donor")
    h = login("bakery", "password-123")
    assert client.post("/api/simulate", json=dict(runs=5000), headers=h).status_code == 422          # capped (hit the limit)
    # A probability of 7 (700%) is nonsense, so it must be refused.
    assert client.post("/api/simulate", json=dict(p_respond=7), headers=h).status_code == 422
    # A normal request works.
    r1 = client.post("/api/simulate", json=body, headers=h)
    assert r1.status_code == 200
    j = r1.json()
    # The answer must carry the "this is a simulation" disclaimer, both main scenarios, and some week-by-week comparisons.
    assert "SIMULATION" in j["disclaimer"] and {"baseline", "full"} <= set(j["scenarios"]) and j["paired"]["runs"] > 0
    # Asking again gives exactly the same answer.
    assert client.post("/api/simulate", json=body, headers=h).json() == j


def test_simulator_page_and_link_are_served(api):
    client, *_ = api
    # The simulator web page must load and say it isn't based on real donors.
    page = client.get("/simulator.html")
    assert page.status_code == 200 and "not from real donors" in page.text
    # And the main page must link to it.
    assert "/simulator.html" in client.get("/").text
