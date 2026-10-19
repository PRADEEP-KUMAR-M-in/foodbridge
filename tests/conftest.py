# Shared test setup that pytest picks up automatically for every test file in this folder.
import pytest  # the testing tool


@pytest.fixture(autouse=True)  # autouse = this runs before EVERY test without anyone asking for it
def _no_exclusive_offers_by_default(monkeypatch):
    """Most tests assume any eligible recipient can claim right away, so first-dibs offers are switched off by default.
    The cascade tests turn them back on."""
    # Set the first-dibs window to 0 minutes (= off) just for the length of this test; monkeypatch undoes it afterwards.
    monkeypatch.setenv("FOODBRIDGE_OFFER_WINDOW_MIN", "0")
