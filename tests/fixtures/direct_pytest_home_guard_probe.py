"""Explicit-only probe for the repository direct-pytest HOME guard.

The filename intentionally does not match pytest's default ``test_*.py``
pattern. Tests invoke it by exact path so a crashed parallel run cannot leave
behind a dynamically discoverable probe in the checkout.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hermes_constants import get_real_home


MODE = os.environ.get("HERMES_DIRECT_PROBE_MODE", "")
HOME_AT_COLLECTION = Path.home()
REAL_HOME_AT_COLLECTION = Path(get_real_home())
HERMES_HOME_AT_COLLECTION = Path(os.environ["HERMES_HOME"])

if MODE == "home":
    handoff = Path(os.environ["HERMES_DIRECT_PROBE_HANDOFF"])
    handoff.write_text(
        json.dumps(
            {
                "home": str(HOME_AT_COLLECTION),
                "real_home": str(REAL_HOME_AT_COLLECTION),
                "hermes_home": str(HERMES_HOME_AT_COLLECTION),
            }
        ),
        encoding="utf-8",
    )


def test_direct_collection_home_isolated() -> None:
    if MODE != "home":
        pytest.skip("home probe not selected")
    assert REAL_HOME_AT_COLLECTION == HOME_AT_COLLECTION
    assert HERMES_HOME_AT_COLLECTION == HOME_AT_COLLECTION / ".hermes"


def test_direct_live_write_is_detected() -> None:
    if MODE != "drift":
        pytest.skip("drift probe not selected")
    live = (
        Path(os.environ["HERMES_TEST_REAL_HOME"])
        / ".hermes"
        / "auth.json"
    )
    live.write_text("after\n", encoding="utf-8")
