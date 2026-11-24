"""FastAPI application: UPS-powered safe shutdown drill for shared storage."""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from .planner import (
    Action,
    Device,
    Plan,
    PlanError,
    compute_plan,
    device_state,
    sample_devices,
    SAMPLE_ENERGY_WH,
    SAMPLE_SHORTFALL_ENERGY_WH,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title="UPS 安全停机演练",
    description="机房断电后，UPS 余电能否支撑共享存储安全停机？",
    version="1.1.0",
)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def no_store_for_sensitive_pages(request, call_next):
    """One-time links and operator tokens must never be served from a cache."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") or path.startswith("/handover/"):
        response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class DeviceIn(BaseModel):
    id: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(..., min_length=1, max_length=60)
    power_w: int = Field(..., ge=0, le=1_000_000)
    shutdown_min: int = Field(..., ge=1, le=240)
    depends_on: List[str] = Field(default_factory=list)

    @field_validator("depends_on")
    @classmethod
    def unique_deps(cls, value: List[str]) -> List[str]:
        if len(set(value)) != len(value):
            raise ValueError("依赖列表有重复项")
        return value

    def to_device(self) -> Device:
        return Device(
            id=self.id,
            name=self.name.strip(),
            power_w=self.power_w,
            shutdown_min=self.shutdown_min,
            depends_on=tuple(self.depends_on),
        )


class DrillCreate(BaseModel):
    devices: List[DeviceIn] = Field(..., min_length=1, max_length=6)
    residual_wh: float = Field(..., ge=0, le=1_000_000)


class DrillRevise(BaseModel):
    residual_wh: float = Field(..., ge=0, le=1_000_000)


class DrillAdvance(BaseModel):
    minutes: int = Field(1, ge=1, le=2000)
    to_next_event: bool = False


class HandoverAccept(BaseModel):
    view_version: int = Field(..., ge=0)


class PreviewRequest(BaseModel):
    devices: List[DeviceIn] = Field(..., min_length=1, max_length=6)
    residual_wh: float = Field(..., ge=0, le=1_000_000)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclass
class HandoverInvite:
    """One-time shift-handover link issued by the current operator."""

    token: str
    created: float
    created_version: int
    created_minute: int
    created_step: int
    used: bool = False
    revoked: bool = False
    used_at: Optional[int] = None
    used_wall: Optional[float] = None
    handover_no: Optional[int] = None


@dataclass
class Session:
    id: str
    devices: List[Device]
    plan: Plan
    now: int
    residual_wh: float
    issued: List[Action] = field(default_factory=list)
    history: List[dict] = field(default_factory=list)
    events: List[dict] = field(default_factory=list)
    revision: Optional[dict] = None
    revision_count: int = 0
    created: float = field(default_factory=time.time)
    version: int = 0
    # Chronological drill step: bumped by every advance/revise operation so the
    # event log can say exactly at which step something (e.g. a handover)
    # happened, even when several events share the same drill minute.
    step: int = 0
    # Ownership handover.  ``owner_token`` authenticates the operator window
    # that may mutate the drill; handing over gives the successor a new token
    # and invalidates the old one.  ``owner_epoch`` is bumped on every
    # handover so an in-flight request from the old page can be rejected at
    # commit time instead of overwriting the successor's drill.
    owner_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    owner_epoch: int = 0
    handover_count: int = 0
    advance_count: int = 0
    invites: Dict[str, HandoverInvite] = field(default_factory=dict, repr=False)
    handovers: List[dict] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    @property
    def feasible(self) -> bool:
        return self.plan.feasible

    @property
    def complete(self) -> bool:
        return self.feasible and self.now >= self.plan.makespan

    @property
    def status(self) -> str:
        if not self.feasible:
            return "shortage"
        if self.complete:
            return "complete"
        if self.now == 0:
            return "ready"
        return "running"

    @property
    def handed_over(self) -> bool:
        return self.handover_count > 0

    def draw_at(self, minute: int) -> int:
        """Power still being drawn at ``minute`` (waiting + stopping devices)."""
        return sum(
            d.power_w
            for d in self.devices
            if self.plan.action_for(d.id).end > minute
        )

    def log(self, kind: str, **payload) -> None:
        self.events.append(
            {"at": self.now, "step": self.step, "kind": kind, **payload}
        )
        self.events = self.events[-100:]

    def snapshot(self) -> dict:
        return {
            "minute": self.now,
            "residual_wh": round(max(0.0, self.residual_wh), 3),
            "draw_w": self.draw_at(self.now) if self.feasible else 0,
        }

    def is_owner(self, token: Optional[str]) -> bool:
        """Whether ``token`` may control the drill.

        For backwards compatibility a token-less request is accepted until the
        first handover (older API clients/tests never received a token).  After
        a handover only the successor's token is accepted, so the outgoing
        window is read-only.
        """
        if token is not None and secrets.compare_digest(token, self.owner_token):
            return True
        if token is None and not self.handed_over:
            return True
        return False

    def freeze_effective(self) -> List[Action]:
        """Snapshot commands that have already taken effect at ``now``.

        A command is effective once its start minute has been reached; its
        completion time is then fixed.  The plan's actions are marked frozen
        so the UI and any later re-plan agree on what is already committed.
        """
        if self.now == 0:
            # Nothing has been executed yet: at minute 0 the plan is still
            # fully re-plannable, so no command is treated as issued.
            return []
        issued = sorted(
            {
                a.device_id: Action(a.device_id, a.start, a.end, True)
                for a in self.plan.actions
                if a.start <= self.now
            }.values(),
            key=lambda a: (a.start, a.device_id),
        )
        frozen_by_id = {a.device_id: a for a in issued}
        self.plan.actions = [
            frozen_by_id.get(a.device_id, a) for a in self.plan.actions
        ]
        self.plan.frozen = list(issued)
        self.issued = issued
        return issued

    def pending_invite(self) -> Optional[HandoverInvite]:
        for invite in self.invites.values():
            if not invite.used and not invite.revoked:
                return invite
        return None


_SESSIONS: "OrderedDict[str, Session]" = OrderedDict()
_LOCK = threading.Lock()
_MAX_SESSIONS = 200


def _drop_handover_tokens(session_id: str) -> None:
    """Forget token -> session pointers; ``_LOCK`` must be held."""
    for token, owner in [
        (t, sid) for t, sid in _HANDOVER_TOKENS.items() if sid == session_id
    ]:
        _HANDOVER_TOKENS.pop(token, None)


def _store(session: Session) -> None:
    with _LOCK:
        _SESSIONS[session.id] = session
        _SESSIONS.move_to_end(session.id)
        while len(_SESSIONS) > _MAX_SESSIONS:
            evicted_id, _ = _SESSIONS.popitem(last=False)
            _drop_handover_tokens(evicted_id)


def _get(session_id: str) -> Session:
    with _LOCK:
        session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="演练会话不存在或已过期")
    return session


def _plan_for(
    devices: List[Device], residual: float, now: int, issued: List[Action]
) -> Plan:
    try:
        return compute_plan(devices, residual, now=now, frozen=issued)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _projection(session: Session) -> List[dict]:
    """Residual-energy curve from the current minute to the last stop."""
    series: List[dict] = []
    if not session.feasible:
        return series
    remaining = session.residual_wh
    for minute in range(session.now, max(session.plan.makespan, session.now) + 1):
        draw = session.draw_at(minute)
        series.append(
            {
                "minute": minute,
                "residual_wh": round(max(0.0, remaining), 3),
                "draw_w": draw,
            }
        )
        remaining -= draw / 60.0
    return series


def _remaining_need(session: Session) -> float:
    """Energy still required from the current minute until the last stop."""
    if not session.feasible:
        return 0.0
    watt_minutes = sum(
        d.power_w * max(0, session.plan.action_for(d.id).end - session.now)
        for d in session.devices
    )
    return watt_minutes / 60.0


def _state(
    session: Session,
    *,
    as_viewer: Optional[HandoverInvite] = None,
    requester_token: Optional[str] = None,
) -> dict:
    """Serialise a session.

    With ``as_viewer`` the payload is the pre-acceptance handover view: it is
    read-only, carries no operator token, and says whether the successor has
    to look at it again (the drill moved after a previous view).
    """
    plan = session.plan
    upcoming = sorted(
        {a.start for a in plan.actions if a.start > session.now}
        | {a.end for a in plan.actions if a.end > session.now}
    )
    payload = {
        "id": session.id,
        "status": session.status,
        "now": session.now,
        "version": session.version,
        "step": session.step,
        "owner_epoch": session.owner_epoch,
        "feasible": session.feasible,
        "residual_wh": round(max(0.0, session.residual_wh), 3),
        "shortfall_wh": round(plan.shortfall_wh, 3) if not session.feasible else 0.0,
        "required_wh": round(
            plan.min_energy_wh if not session.feasible else _remaining_need(session), 3
        ),
        "devices": [
            {
                "id": d.id,
                "name": d.name,
                "power_w": d.power_w,
                "shutdown_min": d.shutdown_min,
                "depends_on": list(d.depends_on),
            }
            for d in session.devices
        ],
        "plan": plan.as_dict(),
        "device_states": device_state(session.devices, plan, session.now),
        "issued": [a.as_dict() for a in session.issued],
        "next_event_minute": upcoming[0] if upcoming else None,
        "can_advance": session.feasible and not session.complete,
        "history": session.history,
        "projection": _projection(session),
        "revision": session.revision,
        "revision_count": session.revision_count,
        "events": session.events,
        "handover_count": session.handover_count,
        "handovers": session.handovers,
    }
    if as_viewer is not None:
        pending = session.pending_invite()
        payload.update(
            {
                "role": "successor-preview",
                "read_only": True,
                "view_version": session.version,
                "view_step": session.step,
                "handover_no": as_viewer.handover_no,
                "invite_created_minute": as_viewer.created_minute,
                "invite_created_version": as_viewer.created_version,
                "pending_invite": pending is not None
                and pending.token == as_viewer.token,
            }
        )
    else:
        payload.update(
            {
                "role": "successor" if session.handed_over else "operator",
                "read_only": not session.is_owner(requester_token),
                "handover": session.handovers[-1] if session.handovers else None,
            }
        )
    return payload


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/handover/{token}", include_in_schema=False)
def handover_page(token: str) -> FileResponse:
    """One-time handover page: preview, confirm, then continue the drill."""
    return FileResponse(STATIC_DIR / "handover.html")


@app.get("/api/health")
def health() -> dict:
    with _LOCK:
        count = len(_SESSIONS)
    return {"ok": True, "sessions": count}


@app.get("/api/sample")
def sample() -> dict:
    devices = sample_devices()
    plan = compute_plan(devices, SAMPLE_ENERGY_WH)
    tight = compute_plan(devices, SAMPLE_SHORTFALL_ENERGY_WH)
    return {
        "devices": [d.as_dict() for d in devices],
        "residual_wh": SAMPLE_ENERGY_WH,
        "tight_residual_wh": SAMPLE_SHORTFALL_ENERGY_WH,
        "note": (
            "断电后，Web/业务应用先停机，随后数据库与备份服务器，最后共享存储。"
            "样例余电足以完成安全停机；把余电改成 250 Wh 可看到缺口演示。"
        ),
        "expected": {
            "makespan": plan.makespan,
            "energy_needed_wh": round(plan.energy_needed_wh, 3),
            "order": plan.order,
            "shortfall_at_tight_wh": round(tight.shortfall_wh, 3),
        },
    }


@app.post("/api/preview")
def preview(payload: PreviewRequest) -> dict:
    devices = [d.to_device() for d in payload.devices]
    try:
        plan = compute_plan(devices, payload.residual_wh, now=0)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "plan": plan.as_dict(),
        "device_states": device_state(devices, plan, 0),
        "can_advance": plan.feasible,
        "shortfall_wh": round(plan.shortfall_wh, 3),
        "required_wh": round(plan.min_energy_wh, 3),
    }


@app.post("/api/drills", status_code=201)
def create_drill(payload: DrillCreate) -> dict:
    devices = [d.to_device() for d in payload.devices]
    try:
        plan = compute_plan(devices, payload.residual_wh, now=0)
    except PlanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    session = Session(
        id=uuid.uuid4().hex,
        devices=devices,
        plan=plan,
        now=0,
        residual_wh=payload.residual_wh,
    )
    session.history.append(session.snapshot())
    session.log(
        "create",
        feasible=plan.feasible,
        residual_wh=round(payload.residual_wh, 3),
        shortfall_wh=round(plan.shortfall_wh, 3),
    )
    _store(session)
    state = _state(session)
    state["owner_token"] = session.owner_token
    return state


@app.get("/api/drills/{session_id}")
def get_drill(
    session_id: str,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    """Read a drill.

    The operator token is optional so the page can always poll for display;
    ``read_only`` in the response tells the outgoing window it may no longer
    mutate the drill.
    """
    session = _get(session_id)
    with session.lock:
        state = _state(session, requester_token=x_operator_token)
    if state["read_only"]:
        state["read_only_reason"] = "演练已交接给接班人，此页面只读"
    return state


# ---------------------------------------------------------------------------
# Shift handover
# ---------------------------------------------------------------------------

# Tokens are unguessable; this index lets a successor open a bare
# ``/handover/<token>`` link without knowing the drill id.
_HANDOVER_TOKENS: Dict[str, str] = {}


def _register_handover_token(token: str, session_id: str) -> None:
    with _LOCK:
        _HANDOVER_TOKENS[token] = session_id


def _session_for_token(token: str) -> Tuple[Session, HandoverInvite]:
    with _LOCK:
        session_id = _HANDOVER_TOKENS.get(token)
    if session_id is None:
        raise HTTPException(status_code=404, detail="交接链接无效或已过期")
    session = _get(session_id)
    invite = session.invites.get(token)
    if invite is None:
        raise HTTPException(status_code=404, detail="交接链接无效或已过期")
    return session, invite


def _owner_guard(
    session: Session, token: Optional[str], epoch: Optional[int]
) -> None:
    """Reject a mutation from a window that no longer owns the drill."""
    if not session.is_owner(token):
        raise HTTPException(
            status_code=403,
            detail="演练已交接给接班人，此页面为只读；请刷新或关闭此页",
        )
    if epoch is not None and epoch != session.owner_epoch:
        raise HTTPException(
            status_code=409,
            detail="演练刚刚完成交接，旧页面的操作未写入演练",
        )


def _ensure_live(invite: HandoverInvite) -> None:
    if invite.used:
        raise HTTPException(
            status_code=409,
            detail="该交接链接已被使用，一个链接只能成功交接一次",
        )
    if invite.revoked:
        raise HTTPException(
            status_code=409, detail="该交接链接已被新链接替代，请向原值班员索取新链接"
        )


@app.post("/api/drills/{session_id}/handovers", status_code=201)
def create_handover(
    session_id: str,
    x_operator_token: Optional[str] = Header(default=None),
    x_owner_epoch: Optional[int] = Header(default=None),
) -> dict:
    """Issue a one-time handover link for the current shift.

    Issuing a new link revokes any earlier unused link, so at most one
    successor can be invited at a time.  The outgoing operator keeps control
    (and may keep advancing / re-recording energy) until the link is accepted.
    """
    session = _get(session_id)
    with session.lock:
        _owner_guard(session, x_operator_token, x_owner_epoch)
        if not session.feasible:
            raise HTTPException(
                status_code=409,
                detail="UPS 余电不足、演练处于暂停状态，暂不能交接",
            )
        for other in session.invites.values():
            if not other.used:
                other.revoked = True
        # Issuing a link is logged like any other drill operation, so the event
        # list has a distinct step for it (the handover itself gets its own).
        session.version += 1
        session.step += 1
        token = secrets.token_urlsafe(32)
        invite = HandoverInvite(
            token=token,
            created=time.time(),
            created_version=session.version,
            created_minute=session.now,
            created_step=session.step,
            handover_no=session.handover_count + 1,
        )
        session.invites[token] = invite
        session.log("handover_invite", handover_no=invite.handover_no)
        _register_handover_token(token, session.id)
        state = _state(session, requester_token=x_operator_token)
    return {
        "token": token,
        "path": f"/handover/{token}",
        "handover_no": invite.handover_no,
        "created_minute": session.now,
        "created_step": session.step,
        "session": state,
    }


def _view_payload(
    session: Session,
    invite: HandoverInvite,
    last_view_version: Optional[int],
    *,
    force_stale: bool = False,
) -> dict:
    state = _state(session, as_viewer=invite)
    state["viewer_token"] = invite.token
    state["changed_since_invite"] = session.version != invite.created_version
    # A fresh look at the live state is never "stale"; passing the version the
    # successor last read lets the page detect a change while they were
    # reading and ask for confirmation again.
    state["stale"] = force_stale or (
        last_view_version is not None and last_view_version != session.version
    )
    if last_view_version is not None:
        state["last_view_version"] = last_view_version
    return state


@app.get("/api/handovers/{token}")
def view_handover(
    token: str,
    x_view_version: Optional[int] = Header(default=None),
) -> dict:
    """Successor's read-only pre-acceptance view, always the live state.

    Viewing consumes nothing.  Passing ``X-View-Version`` marks the response
    ``stale`` when the drill moved since that view, so the page can show the
    new state and ask for confirmation again.
    """
    session, invite = _session_for_token(token)
    with session.lock:
        _ensure_live(invite)
        return _view_payload(session, invite, x_view_version)


@app.get("/api/drills/{session_id}/handovers/{token}")
def view_handover_scoped(
    session_id: str,
    token: str,
    x_view_version: Optional[int] = Header(default=None),
) -> dict:
    session, invite = _session_for_token(token)
    if session.id != session_id:
        raise HTTPException(status_code=404, detail="交接链接无效或已过期")
    with session.lock:
        _ensure_live(invite)
        return _view_payload(session, invite, x_view_version)


def _commit_handover(
    session: Session, invite: HandoverInvite, view_version: int
) -> JSONResponse:
    """Consume the invite once and move ownership to the successor."""
    if not session.feasible:
        raise HTTPException(
            status_code=409,
            detail="UPS 余电不足、演练处于暂停状态，暂不能接手；请原值班员补录后再试",
        )
    if view_version != session.version:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "演练状态在查看后已变化，请查看最新状态后重新确认",
                "reason": "stale",
                "view_version": view_version,
                "current_version": session.version,
                "state": _view_payload(session, invite, None, force_stale=True),
            },
        )
    # Operations that had already taken effect (started at or before the
    # current minute) travel with the drill.  Everything still in the future,
    # including the outgoing page's not-yet-applied work, is the successor's
    # to schedule.  No re-planning is needed to accept: the successor does not
    # wait for the old page's residual-energy re-plan.
    issued = session.freeze_effective()
    session.owner_token = secrets.token_urlsafe(32)
    session.owner_epoch += 1
    session.handover_count += 1
    invite.used = True
    invite.used_at = session.now
    invite.used_wall = time.time()
    session.version += 1
    session.step += 1
    remaining = [
        a.start for a in session.plan.actions if a.start > session.now
    ] + [a.end for a in session.plan.actions if a.end > session.now]
    record = {
        "handover_no": session.handover_count,
        "minute": session.now,
        "step": session.step,
        "version": session.version,
        "issued_count": len(issued),
        "issued": [a.as_dict() for a in issued],
        "invite_created_minute": invite.created_minute,
        "invite_created_step": invite.created_step,
        "next_event_minute": min(remaining) if remaining else session.plan.makespan,
        "successor_role": "operator",
    }
    session.handovers.append(record)
    session.handovers = session.handovers[-50:]
    session.log(
        "handover",
        handover_no=session.handover_count,
        minute=session.now,
        issued_count=len(issued),
        invite_created_minute=invite.created_minute,
        invite_created_step=invite.created_step,
    )
    state = _state(session, requester_token=session.owner_token)
    state["owner_token"] = session.owner_token
    state["handover"] = record
    state["handover_history"] = session.handovers
    return JSONResponse(status_code=200, content=state)


@app.post("/api/handovers/{token}/accept")
def accept_handover(token: str, payload: HandoverAccept) -> JSONResponse:
    """Consume the link exactly once and transfer control atomically.

    ``view_version`` is the version the successor confirmed against.  The
    acceptance, ownership swap and snapshot of already-effective commands are
    committed together under the session lock, so a re-plan that the old page
    was still computing cannot change the drill afterwards.
    """
    session, invite = _session_for_token(token)
    with session.lock:
        _ensure_live(invite)
        return _commit_handover(session, invite, payload.view_version)


@app.post("/api/drills/{session_id}/handovers/{token}/accept")
def accept_handover_scoped(
    session_id: str, token: str, payload: HandoverAccept
) -> JSONResponse:
    session, invite = _session_for_token(token)
    if session.id != session_id:
        raise HTTPException(status_code=404, detail="交接链接无效或已过期")
    with session.lock:
        _ensure_live(invite)
        return _commit_handover(session, invite, payload.view_version)


@app.post("/api/drills/{session_id}/advance")
def advance_drill(
    session_id: str,
    payload: DrillAdvance,
    x_operator_token: Optional[str] = Header(default=None),
    x_owner_epoch: Optional[int] = Header(default=None),
) -> dict:
    session = _get(session_id)
    # The whole mutation is serialised on the session lock, and ownership is
    # re-checked under that lock right before committing, so an old page whose
    # request was already in flight cannot apply its stale work after a
    # handover.
    with session.lock:
        _owner_guard(session, x_operator_token, x_owner_epoch)
        if not session.feasible:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"UPS 余电不足 {session.plan.shortfall_wh:.1f} Wh，"
                    "请先补录当前余电再推进"
                ),
            )
        if session.complete:
            return _state(session, requester_token=x_operator_token)

        if payload.to_next_event:
            candidates = [
                a.start for a in session.plan.actions if a.start > session.now
            ] + [a.end for a in session.plan.actions if a.end > session.now]
            target = min(candidates) if candidates else session.plan.makespan
        else:
            target = session.now + payload.minutes
        target = min(target, session.plan.makespan)

        step_energy = 0.0
        while session.now < target:
            draw = session.draw_at(session.now)
            step_energy += draw / 60.0
            session.residual_wh = max(0.0, session.residual_wh - draw / 60.0)
            session.now += 1
            session.history.append(session.snapshot())

        # Commands whose start minute has been reached are now immutable; the rest
        # of the plan is intentionally left untouched until the operator revises
        # the residual energy (that is what triggers a re-plan).
        session.freeze_effective()
        session.version += 1
        session.step += 1
        session.advance_count += 1
        if session.complete:
            session.log("complete", residual_wh=round(session.residual_wh, 3))
        else:
            session.log("advance", to=session.now, spent_wh=round(step_energy, 3))
        return _state(session, requester_token=x_operator_token)


@app.post("/api/drills/{session_id}/revise")
def revise_drill(
    session_id: str,
    payload: DrillRevise,
    x_operator_token: Optional[str] = Header(default=None),
    x_owner_epoch: Optional[int] = Header(default=None),
) -> dict:
    """Record a fresh residual-energy reading and re-plan unstarted work.

    Planning can take a moment, so it is done *outside* the session lock: a
    successor accepting a handover at the same time never waits for this
    computation.  The result is committed only if, when the lock is taken
    again, this window still owns the drill and nothing else changed the
    session in the meantime.  In particular, a re-plan computed by the old
    page cannot land after the handover has been accepted.
    """
    session = _get(session_id)

    with session.lock:
        _owner_guard(session, x_operator_token, x_owner_epoch)
        base_version = session.version
        now = session.now
        issued = list(session.issued)
        old_plan = session.plan

    new_plan = _plan_for(session.devices, payload.residual_wh, now, issued)

    with session.lock:
        _owner_guard(session, x_operator_token, x_owner_epoch)
        if session.version != base_version:
            raise HTTPException(
                status_code=409,
                detail="演练状态在补录计算期间已变化，本次补录未写入，请重试",
            )

        issued_ids = {a.device_id for a in issued}
        preserved = []
        rescheduled = []
        for action in new_plan.actions:
            old = old_plan.action_for(action.device_id)
            entry = {
                "device_id": action.device_id,
                "name": next(
                    d.name for d in session.devices if d.id == action.device_id
                ),
                "start": action.start,
                "end": action.end,
                "old_start": old.start,
                "old_end": old.end,
                "status": "stopped"
                if action.end <= session.now
                else ("running" if action.start < session.now else "waiting"),
            }
            if action.device_id in issued_ids:
                preserved.append(entry)
            elif (old.start, old.end) != (action.start, action.end):
                rescheduled.append(entry)

        session.residual_wh = payload.residual_wh
        session.plan = new_plan
        session.revision_count += 1
        session.version += 1
        session.step += 1
        session.revision = {
            "count": session.revision_count,
            "at_minute": session.now,
            "reported_residual_wh": round(payload.residual_wh, 3),
            "preserved": preserved,
            "rescheduled": rescheduled,
            "feasible": new_plan.feasible,
            "shortfall_wh": round(new_plan.shortfall_wh, 3),
            "required_wh": round(
                new_plan.energy_needed_wh if new_plan.feasible else new_plan.min_energy_wh,
                3,
            ),
            "preserved_finish_times": all(
                new_plan.action_for(a.device_id).end == a.end for a in session.issued
            ),
            "all_frozen": len(issued_ids) > 0
            and all(a.frozen for a in new_plan.actions if a.device_id in issued_ids),
        }
        session.history.append(session.snapshot())
        session.log(
            "revise",
            residual_wh=round(payload.residual_wh, 3),
            preserved=len(preserved),
            rescheduled=len(rescheduled),
            feasible=new_plan.feasible,
            shortfall_wh=round(new_plan.shortfall_wh, 3),
        )
        return _state(session, requester_token=x_operator_token)


@app.delete("/api/drills/{session_id}")
def delete_drill(
    session_id: str,
    x_operator_token: Optional[str] = Header(default=None),
    x_owner_epoch: Optional[int] = Header(default=None),
) -> dict:
    session = _get(session_id)
    with session.lock:
        _owner_guard(session, x_operator_token, x_owner_epoch)
        with _LOCK:
            existed = _SESSIONS.pop(session_id, None) is not None
            if existed:
                _drop_handover_tokens(session_id)
    if not existed:
        raise HTTPException(status_code=404, detail="演练会话不存在或已过期")
    return {"deleted": True}
