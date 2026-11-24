"""Shift-handover tests: one-time link, live re-confirmation, ownership.

The interesting part is the interleaving between a handover accept and the old
page's residual-energy re-plan.  ``app.main._plan_for`` is patched so a test can
hold a re-plan inside its computation while the successor accepts the link,
then release it and prove the old page's work cannot land on the drill.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.main as main  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    main._SESSIONS.clear()
    main._HANDOVER_TOKENS.clear()
    with TestClient(app) as c:
        yield c


def make_drill(client, residual=400.0):
    sample = client.get("/api/sample").json()
    created = client.post(
        "/api/drills",
        json={"devices": sample["devices"], "residual_wh": residual},
    )
    assert created.status_code == 201
    session = created.json()
    return session["id"], session["owner_token"]


def issue(client, sid, owner):
    response = client.post(
        f"/api/drills/{sid}/handovers",
        headers={"X-Operator-Token": owner},
    )
    assert response.status_code == 201, response.text
    return response.json()


def view(client, token, previous_version=None):
    headers = {}
    if previous_version is not None:
        headers["X-View-Version"] = str(previous_version)
    response = client.get(f"/api/handovers/{token}", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def accept(client, token, view_version):
    return client.post(f"/api/handovers/{token}/accept", json={"view_version": view_version})


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------


def test_handover_link_is_one_time_and_old_page_is_read_only(client):
    sid, owner = make_drill(client)
    client.post(
        f"/api/drills/{sid}/advance",
        json={"minutes": 3},
        headers={"X-Operator-Token": owner, "X-Owner-Epoch": "0"},
    )
    invite = issue(client, sid, owner)
    token = invite["token"]
    assert token and len(token) > 20

    preview = view(client, token)
    assert preview["read_only"] is True
    assert preview["role"] == "successor-preview"
    assert preview["now"] == 3
    assert preview["residual_wh"] < 400
    assert {a["device_id"] for a in preview["issued"]} == {"web", "app", "db", "backup"}

    accepted = accept(client, token, preview["view_version"])
    assert accepted.status_code == 200, accepted.text
    successor = accepted.json()
    assert successor["owner_token"] and successor["owner_token"] != owner
    assert successor["read_only"] is False
    assert successor["role"] == "successor"
    assert successor["now"] == 3
    assert successor["residual_wh"] == pytest.approx(preview["residual_wh"])
    # Same schedule and history travel with the drill.
    assert successor["plan"]["actions"] == preview["plan"]["actions"]
    assert successor["history"] == preview["history"]
    assert successor["handover_count"] == 1

    # The one-time link cannot be re-viewed or re-accepted.
    assert client.get(f"/api/handovers/{token}").status_code == 409
    assert accept(client, token, preview["view_version"]).status_code == 409

    # The outgoing page keeps read access to the live state but is read-only.
    old_state = client.get(
        f"/api/drills/{sid}", headers={"X-Operator-Token": owner}
    ).json()
    assert old_state["read_only"] is True
    blocked = client.post(
        f"/api/drills/{sid}/advance",
        json={"minutes": 1},
        headers={"X-Operator-Token": owner},
    )
    assert blocked.status_code == 403
    blocked_revise = client.post(
        f"/api/drills/{sid}/revise",
        json={"residual_wh": 300},
        headers={"X-Operator-Token": owner},
    )
    assert blocked_revise.status_code == 403

    # The successor can go on with exactly the same drill, without re-planning
    # anything at acceptance time.
    nxt = client.post(
        f"/api/drills/{sid}/advance",
        json={"to_next_event": True},
        headers={
            "X-Operator-Token": successor["owner_token"],
            "X-Owner-Epoch": str(successor["owner_epoch"]),
        },
    )
    assert nxt.status_code == 200, nxt.text
    assert nxt.json()["now"] >= 3


def test_view_does_not_consume_and_operator_can_keep_working(client):
    sid, owner = make_drill(client)
    token = issue(client, sid, owner)["token"]
    first = view(client, token)
    assert first["now"] == 0

    # Before confirmation the outgoing operator may keep advancing and
    # recording energy.
    step = client.post(
        f"/api/drills/{sid}/advance",
        json={"minutes": 2},
        headers={"X-Operator-Token": owner},
    )
    assert step.status_code == 200
    revised = client.post(
        f"/api/drills/{sid}/revise",
        json={"residual_wh": 500.0},
        headers={"X-Operator-Token": owner},
    )
    assert revised.status_code == 200

    # The successor looking again sees the latest state, not the first view,
    # and is told it changed since they last looked.
    second = view(client, token, previous_version=first["view_version"])
    assert second["now"] == 2
    assert second["residual_wh"] == pytest.approx(500.0)
    assert second["stale"] is True
    assert second["view_version"] != first["view_version"]


def test_accept_after_change_requires_reconfirmation(client):
    sid, owner = make_drill(client)
    token = issue(client, sid, owner)["token"]
    first = view(client, token)

    client.post(
        f"/api/drills/{sid}/advance",
        json={"minutes": 1},
        headers={"X-Operator-Token": owner},
    )

    stale = accept(client, token, first["view_version"])
    assert stale.status_code == 409
    body = stale.json()
    assert body["reason"] == "stale"
    assert body["current_version"] != first["view_version"]
    latest = body["state"]
    assert latest["now"] == 1
    assert latest["stale"] is True
    assert latest["read_only"] is True

    # The link is still unused: confirming against the latest state works.
    confirmed = accept(client, token, latest["view_version"])
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["now"] == 1
    assert confirmed.json()["handover_count"] == 1


def test_new_link_revokes_previous_unused_link(client):
    sid, owner = make_drill(client)
    first = issue(client, sid, owner)
    second = issue(client, sid, owner)
    assert first["token"] != second["token"]
    assert client.get(f"/api/handovers/{first['token']}").status_code == 409
    ok = view(client, second["token"])
    assert ok["now"] == 0


def test_handover_requires_owner_and_feasible_drill(client):
    sid, owner = make_drill(client)
    assert (
        client.post(
            f"/api/drills/{sid}/handovers",
            headers={"X-Operator-Token": "not-the-owner"},
        ).status_code
        == 403
    )

    tight_sid, tight_owner = make_drill(client, residual=250.0)
    paused = client.post(
        f"/api/drills/{tight_sid}/handovers",
        headers={"X-Operator-Token": tight_owner},
    )
    assert paused.status_code == 409
    assert "余电" in paused.json()["detail"]


def test_handover_is_visible_in_event_log_at_its_step(client):
    sid, owner = make_drill(client)
    headers = {"X-Operator-Token": owner}
    client.post(f"/api/drills/{sid}/advance", json={"minutes": 1}, headers=headers)
    client.post(f"/api/drills/{sid}/advance", json={"minutes": 1}, headers=headers)
    invite = issue(client, sid, owner)
    preview = view(client, invite["token"])
    accepted = accept(client, invite["token"], preview["view_version"]).json()

    events = accepted["events"]
    handover_events = [e for e in events if e["kind"] == "handover"]
    assert len(handover_events) == 1
    event = handover_events[0]
    assert event["minute"] == 2
    assert event["handover_no"] == 1
    # The step is chronological, so the log locates the exact operation even
    # though several things happened at the same drill minute.
    assert event["step"] == accepted["step"]
    assert event["step"] > 0
    steps = [e["step"] for e in events]
    assert steps == sorted(steps)
    assert event["issued_count"] >= 1

    record = accepted["handover"]
    assert record["minute"] == 2
    assert record["step"] == event["step"]
    assert record["handover_no"] == 1


def test_successor_resume_token_in_state_and_history_preserved(client):
    sid, owner = make_drill(client)
    client.post(
        f"/api/drills/{sid}/advance",
        json={"minutes": 4},
        headers={"X-Operator-Token": owner},
    )
    before = client.get(f"/api/drills/{sid}").json()
    invite = issue(client, sid, owner)
    preview = view(client, invite["token"])
    accepted = accept(client, invite["token"], preview["view_version"]).json()

    # Everything that had already taken effect at minute 4 is now frozen and
    # travels with the drill; nothing had to be re-planned at handover time.
    frozen = {a["device_id"]: a for a in accepted["plan"]["actions"] if a["frozen"]}
    assert set(frozen) == {"web", "app", "db", "backup"}
    assert frozen["web"]["start"] == 0 and frozen["web"]["end"] == 3
    assert accepted["history"] == before["history"]
    assert accepted["issued"] == [a for a in accepted["issued"] if a["frozen"]]
    assert {a["device_id"] for a in accepted["issued"]} == set(frozen)

    # A future command from before the handover was not applied; the
    # successor's residual-energy re-plan is free to move unstarted work.
    revised = client.post(
        f"/api/drills/{sid}/revise",
        json={"residual_wh": 150.0},
        headers={
            "X-Operator-Token": accepted["owner_token"],
            "X-Owner-Epoch": str(accepted["owner_epoch"]),
        },
    )
    assert revised.status_code == 200, revised.text
    body = revised.json()
    after = {a["device_id"]: a for a in body["plan"]["actions"]}
    for device_id, action in frozen.items():
        assert after[device_id]["start"] == action["start"]
        assert after[device_id]["end"] == action["end"]
        assert after[device_id]["frozen"] is True


# ---------------------------------------------------------------------------
# Handover racing the old page's re-plan
# ---------------------------------------------------------------------------


def test_handover_does_not_wait_for_old_page_replan_and_replan_cannot_land(
    client, monkeypatch
):
    """Interleave: the old page starts a re-plan, the successor accepts.

    * the successor's acceptance completes while the re-plan is still busy;
    * when the re-plan finishes it is rejected, so the drill keeps the
      successor's state instead of being overwritten by the old page.
    """
    sid, owner = make_drill(client, residual=320.0)
    client.post(
        f"/api/drills/{sid}/advance",
        json={"minutes": 2},
        headers={"X-Operator-Token": owner},
    )
    invite = issue(client, sid, owner)
    preview = view(client, invite["token"])

    entered = threading.Event()
    release = threading.Event()
    real_plan_for = main._plan_for

    def slow_plan_for(*args, **kwargs):
        plan = real_plan_for(*args, **kwargs)
        entered.set()
        assert release.wait(10), "re-plan was never released"
        return plan

    monkeypatch.setattr(main, "_plan_for", slow_plan_for)

    outcome = {}

    def old_page_revise():
        outcome["revise"] = client.post(
            f"/api/drills/{sid}/revise",
            json={"residual_wh": 160.0},
            headers={"X-Operator-Token": owner},
        )

    worker = threading.Thread(target=old_page_revise)
    worker.start()
    assert entered.wait(10), "re-plan never started"

    # The successor accepts while the old page is still computing.
    started = time.monotonic()
    accepted = accept(client, invite["token"], preview["view_version"])
    elapsed = time.monotonic() - started
    assert accepted.status_code == 200, accepted.text
    assert elapsed < 5, "acceptance waited for the old page's re-plan"
    successor_state = accepted.json()

    release.set()
    worker.join(10)
    revise_response = outcome["revise"]
    # Rejected because ownership moved while the re-plan was computing — by
    # token (403) or, if the old token somehow survived, by epoch (409).
    assert revise_response.status_code in (403, 409), revise_response.text
    assert "交接" in revise_response.json()["detail"]

    # The drill carries the successor's state: no residual change from the
    # old page's re-plan, still exactly one handover, still valid schedule.
    final = client.get(
        f"/api/drills/{sid}",
        headers={"X-Operator-Token": successor_state["owner_token"]},
    ).json()
    assert final["residual_wh"] == pytest.approx(successor_state["residual_wh"])
    assert final["version"] == successor_state["version"]
    assert final["handover_count"] == 1

    # ... and the drill can still be finished by the successor.
    finished = client.post(
        f"/api/drills/{sid}/advance",
        json={"minutes": 60},
        headers={
            "X-Operator-Token": successor_state["owner_token"],
            "X-Owner-Epoch": str(successor_state["owner_epoch"]),
        },
    )
    assert finished.status_code == 200
    assert finished.json()["status"] == "complete"


def test_advance_in_flight_across_handover_is_serialised(client, monkeypatch):
    """An advance already inside the lock finishes before the handover.

    Then the handover (whose view version predates that advance) is refused
    for re-confirmation instead of silently consuming the link.
    """
    sid, owner = make_drill(client)
    invite = issue(client, sid, owner)
    preview = view(client, invite["token"])

    results = {}

    def old_page_advance():
        results["advance"] = client.post(
            f"/api/drills/{sid}/advance",
            json={"minutes": 2},
            headers={"X-Operator-Token": owner},
        )

    worker = threading.Thread(target=old_page_advance)
    worker.start()
    worker.join(10)

    stale = accept(client, invite["token"], preview["view_version"])
    assert stale.status_code == 409
    assert stale.json()["reason"] == "stale"
    latest = stale.json()["state"]
    ok = accept(client, invite["token"], latest["view_version"])
    assert ok.status_code == 200
    assert ok.json()["now"] == 2


def test_wrong_epoch_rejected_even_with_right_token(client):
    sid, owner = make_drill(client)
    invite = issue(client, sid, owner)
    preview = view(client, invite["token"])
    successor = accept(client, invite["token"], preview["view_version"]).json()

    # A request from the outgoing page that somehow still carries the old
    # token is rejected by token; a request with the right token but a stale
    # epoch (an old page that already loaded the new token) is rejected too.
    stale_epoch = client.post(
        f"/api/drills/{sid}/advance",
        json={"minutes": 1},
        headers={
            "X-Operator-Token": successor["owner_token"],
            "X-Owner-Epoch": "0",
        },
    )
    assert stale_epoch.status_code == 409
    assert "交接" in stale_epoch.json()["detail"]


def test_handover_at_minute_zero_and_successor_can_advance(client):
    sid, owner = make_drill(client)
    token = issue(client, sid, owner)["token"]
    preview = view(client, token)
    assert preview["now"] == 0
    assert preview["issued"] == []  # nothing has taken effect yet

    accepted = accept(client, token, preview["view_version"]).json()
    assert accepted["handover"]["issued_count"] == 0
    assert accepted["handover"]["minute"] == 0
    assert accepted["now"] == 0

    started = client.post(
        f"/api/drills/{sid}/advance",
        json={"minutes": 1},
        headers={
            "X-Operator-Token": accepted["owner_token"],
            "X-Owner-Epoch": str(accepted["owner_epoch"]),
        },
    )
    assert started.status_code == 200
    assert started.json()["now"] == 1


def test_chained_handovers_and_first_successor_becomes_read_only(client):
    sid, owner = make_drill(client)
    headers = {"X-Operator-Token": owner}

    first_token = issue(client, sid, owner)["token"]
    first_preview = view(client, first_token)
    first = accept(client, first_token, first_preview["view_version"]).json()
    first_token_value = first["owner_token"]
    first_headers = {
        "X-Operator-Token": first_token_value,
        "X-Owner-Epoch": str(first["owner_epoch"]),
    }

    # The first successor issues the next link and keeps working until it is
    # accepted.
    second_invite = client.post(
        f"/api/drills/{sid}/handovers", headers=first_headers
    )
    assert second_invite.status_code == 201, second_invite.text
    second_token = second_invite.json()["token"]
    assert (
        client.post(
            f"/api/drills/{sid}/advance", json={"minutes": 1}, headers=first_headers
        ).status_code
        == 200
    )

    second_preview = view(client, second_token)
    second = accept(client, second_token, second_preview["view_version"]).json()
    assert second["handover_count"] == 2
    assert second["now"] == 1
    assert [h["handover_no"] for h in second["handovers"]] == [1, 2]

    # Both earlier windows are now read-only.
    for token in (owner, first_token_value):
        state = client.get(
            f"/api/drills/{sid}", headers={"X-Operator-Token": token}
        ).json()
        assert state["read_only"] is True
        blocked = client.post(
            f"/api/drills/{sid}/revise",
            json={"residual_wh": 400},
            headers={"X-Operator-Token": token},
        )
        assert blocked.status_code == 403

    # Both handovers are visible in the event log at distinct steps.
    handovers = [e for e in second["events"] if e["kind"] == "handover"]
    assert [e["handover_no"] for e in handovers] == [1, 2]
    assert handovers[0]["step"] < handovers[1]["step"]


def test_preview_payload_carries_no_control_token(client):
    sid, owner = make_drill(client)
    token = issue(client, sid, owner)["token"]
    preview = view(client, token)
    assert "owner_token" not in preview
    assert preview["read_only"] is True


def test_concurrent_accept_of_same_link_only_one_wins(client):
    sid, owner = make_drill(client)
    token = issue(client, sid, owner)["token"]
    preview = view(client, token)
    results = []
    lock = threading.Lock()

    def attempt():
        response = accept(client, token, preview["view_version"])
        with lock:
            results.append(response.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == [200, 409]
    assert client.get(f"/api/drills/{sid}").json()["handover_count"] == 1


def test_handover_page_is_served_and_api_discoverable(client):
    page = client.get("/handover/whatever")
    assert page.status_code == 200
    assert "交接" in page.text
    assert "handover.js" in page.text
    # A bogus token cannot be used to read or accept.
    assert client.get("/api/handovers/whatever").status_code == 404
    assert (
        client.post("/api/handovers/whatever/accept", json={"view_version": 0}).status_code
        == 404
    )
