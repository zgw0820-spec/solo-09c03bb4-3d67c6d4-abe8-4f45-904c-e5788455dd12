"use strict";

// ---------------------------------------------------------------------------
// Successor handover page
//
// Opens /handover/<token>, shows the live drill read-only (minute, residual
// energy, issued commands), then transfers control exactly once on confirm.
// If the drill changes while the page is open, the latest state is shown and
// the successor must confirm again.  After accepting, this page continues the
// same drill (same schedule and history) with the successor's own token; the
// outgoing operator's page becomes read-only.
// ---------------------------------------------------------------------------

const HO = {
  token: null,
  view: null,            // last read-only payload from the server
  displayedVersion: null,
  acknowledgedVersion: null, // version the successor explicitly re-confirmed
  busy: false,
  accepted: false,
  ownerToken: null,
  ownerEpoch: null,
  session: null,
  pollTimer: null,
  invite: null,          // link the successor issues for the next shift
};

const $ = (id) => document.getElementById(id);

const STATUS_TEXT = {
  waiting: "未开始",
  starting: "即将开始",
  stopping: "停机中",
  stopped: "已停妥",
};

const STATUS_LABEL = {
  ready: "演练就绪",
  running: "演练进行中",
  complete: "演练已结束",
  shortage: "余电不足 · 暂停",
};

function tokenFromPath() {
  const match = window.location.pathname.match(/^\/handover\/([^/?#]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

function storageKey() {
  return `ups-drill-handover:${HO.token}`;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fmt(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toFixed(digits).replace(/\.0+$/, "");
}

function showError(message, target = "ho-error") {
  const box = $(target);
  if (!box) return;
  box.hidden = !message;
  box.textContent = message || "";
}

function authHeaders(extra = {}) {
  const headers = { "Content-Type": "application/json", ...extra };
  if (HO.ownerToken) headers["X-Operator-Token"] = HO.ownerToken;
  if (HO.ownerEpoch !== null) headers["X-Owner-Epoch"] = String(HO.ownerEpoch);
  return headers;
}

async function api(path, options = {}) {
  const { headers, ...rest } = options;
  const response = await fetch(path, { ...rest, headers: authHeaders(headers) });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok) {
    const error = new Error(
      (payload && payload.detail) || `请求失败（HTTP ${response.status}）`,
    );
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

function deviceName(session, id) {
  const device = session.devices.find((d) => d.id === id);
  return device ? device.name : id;
}

function stopPolling() {
  if (HO.pollTimer) {
    clearInterval(HO.pollTimer);
    HO.pollTimer = null;
  }
}

// ---------------------------------------------------------------------------
// Pre-acceptance preview
// ---------------------------------------------------------------------------

function renderPreview(view) {
  HO.view = view;
  HO.displayedVersion = view.view_version;

  const badge = $("ho-badge");
  badge.hidden = false;
  badge.textContent = STATUS_LABEL[view.status] || view.status;
  badge.className =
    "badge " +
    (view.status === "shortage" ? "bad" : view.status === "complete" ? "ok" : "");

  $("ho-title").textContent = `第 ${view.handover_no || 1} 次交接 · 请核对后接手`;
  const created =
    view.invite_created_minute !== undefined
      ? `链接生成于第 ${view.invite_created_minute} 分钟。`
      : "";
  $("ho-subtitle").textContent =
    `本场演练已进行到第 ${view.now} 分钟，` +
    `当前余电 ${fmt(view.residual_wh)} Wh，` +
    `已下达 ${view.issued.length} 条停机命令。` +
    created +
    "确认后由你继续推进，原值班员页面转为只读。";

  const nextEvent =
    view.next_event_minute === null || view.next_event_minute === undefined
      ? "无"
      : `第 ${view.next_event_minute} 分钟`;
  const draw = view.device_states
    .filter((d) => d.status !== "stopped")
    .reduce((sum, d) => sum + d.power_w, 0);
  $("ho-metrics").innerHTML = `
    <div class="handover-metric"><span>当前分钟</span><strong>${view.now}</strong></div>
    <div class="handover-metric"><span>当前余电</span><strong>${fmt(view.residual_wh)} Wh</strong></div>
    <div class="handover-metric"><span>实时功率</span><strong>${fmt(draw, 0)} W</strong></div>
    <div class="handover-metric"><span>已下达命令</span><strong>${view.issued.length} 条</strong></div>
    <div class="handover-metric"><span>下一事件</span><strong>${nextEvent}</strong></div>`;

  const notice = $("ho-notice");
  if (view.stale) {
    notice.hidden = false;
    notice.innerHTML =
      "⚠ 演练状态在你查看后发生变化，下面显示的是<strong>最新状态</strong>。" +
      "请重新核对当前分钟、余电和已下达命令，再确认接手。";
  } else if (view.changed_since_invite) {
    notice.hidden = false;
    notice.innerHTML =
      "链接生成后演练又有推进，下面显示的是<strong>最新状态</strong>，请核对后确认。";
  } else {
    notice.hidden = true;
    notice.innerHTML = "";
  }

  const issuedBody = $("ho-issued");
  issuedBody.innerHTML = "";
  if (!view.issued.length) {
    issuedBody.innerHTML =
      '<tr><td colspan="4" class="handover-status-line">暂无已生效的命令（第 0 分钟）。</td></tr>';
  } else {
    view.issued.forEach((a) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(deviceName(view, a.device_id))}</td>
        <td class="mono">${a.start} 分钟</td>
        <td class="mono">${a.end} 分钟</td>
        <td><span class="tag stopped">已下达 · 随演练移交</span></td>`;
      issuedBody.appendChild(tr);
    });
  }

  const pending = view.device_states
    .filter((d) => d.start > view.now)
    .sort((a, b) => a.start - b.start || a.end - b.end);
  const pendingBody = $("ho-pending");
  pendingBody.innerHTML = "";
  if (!pending.length) {
    pendingBody.innerHTML =
      '<tr><td colspan="4" class="handover-status-line">没有尚未生效的动作。</td></tr>';
  } else {
    pending.forEach((d) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(d.name)}</td>
        <td class="mono">${d.start} 分钟</td>
        <td class="mono">${d.end} 分钟</td>
        <td><span class="tag ${d.status}">${STATUS_TEXT[d.status] || d.status}</span></td>`;
      pendingBody.appendChild(tr);
    });
  }
  $("ho-pending-title").textContent =
    `尚未生效的动作（${pending.length} 条，接手后由你推进）`;

  const accept = $("ho-accept");
  accept.hidden = false;
  if (view.status === "shortage") {
    accept.disabled = true;
    $("ho-ack").textContent =
      "当前余电不足、演练暂停，此链接暂不可接手；请原值班员补录余电后生成新链接。";
    return;
  }
  const needsReconfirm =
    view.stale && HO.acknowledgedVersion !== view.view_version;
  accept.disabled = HO.busy;
  accept.textContent = needsReconfirm ? "已核对最新状态，重新确认接手" : "确认接手";
  $("ho-ack").textContent = needsReconfirm
    ? `状态已更新到第 ${view.now} 分钟（版本 ${view.view_version}），请核对后重新确认。`
    : `已读取第 ${view.now} 分钟的状态，请确认接手。`;
}

function renderAccepted(session) {
  HO.accepted = true;
  HO.session = session;
  $("ho-drill-panel").hidden = false;
  $("ho-accept").hidden = true;
  $("ho-refresh").hidden = true;
  $("ho-badge").textContent = "已接手";
  $("ho-badge").className = "badge ok";
  const handover = session.handover || {};
  $("ho-title").textContent = `已接手第 ${handover.handover_no || 1} 次交接`;
  $("ho-subtitle").textContent =
    `交接完成于第 ${handover.minute ?? session.now} 分钟` +
    `（第 ${handover.step ?? session.step} 步）。` +
    "已生效命令随演练移交；演练事件列表里可以查到这次交接。";
  $("ho-notice").hidden = true;
  $("ho-ack").textContent = "";
  renderDrill();
}

function renderDrill() {
  const session = HO.session;
  if (!session) return;
  $("ho-clock").textContent = String(session.now);
  const nextEvent =
    session.next_event_minute === null || session.next_event_minute === undefined
      ? "—"
      : `${session.next_event_minute} 分钟`;
  $("ho-drill-metrics").innerHTML = `
    <div class="metric"><span>当前分钟</span><strong>${session.now}</strong></div>
    <div class="metric"><span>当前余电</span><strong>${fmt(session.residual_wh)} Wh</strong></div>
    <div class="metric"><span>后续还需</span><strong>${fmt(session.required_wh)} Wh</strong></div>
    <div class="metric"><span>下一事件</span><strong>${nextEvent}</strong></div>`;

  $("ho-devices").innerHTML = "";
  session.device_states.forEach((d) => {
    const card = document.createElement("div");
    card.className = `device-card ${d.status}`;
    card.innerHTML = `
      ${d.frozen ? '<span class="frozen-flag">命令已下达</span>' : ""}
      <h4>${escapeHtml(d.name)}</h4>
      <div class="meta">
        ${fmt(d.power_w, 0)} W · 停机 ${d.shutdown_min} 分钟<br />
        ${d.start} → ${d.end} 分钟 · ${STATUS_TEXT[d.status] || d.status}
      </div>`;
    $("ho-devices").appendChild(card);
  });

  // Schedule rows come from the same plan and device records the drill uses.
  const planBody = $("ho-plan-rows");
  planBody.innerHTML = "";
  session.device_states.forEach((d) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(d.name)}</td>
      <td class="mono">${fmt(d.power_w, 0)} W</td>
      <td class="mono">${d.shutdown_min} 分钟</td>
      <td class="mono">${d.start}</td>
      <td class="mono">${d.end}</td>
      <td><span class="tag ${d.status}">${STATUS_TEXT[d.status] || d.status}` +
        `${d.frozen ? " · 已下达" : ""}</span></td>`;
    planBody.appendChild(tr);
  });

  const historyBody = $("ho-history-rows");
  historyBody.innerHTML = "";
  (session.history || [])
    .slice()
    .reverse()
    .slice(0, 40)
    .forEach((h) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `<td>${h.minute}</td><td>${fmt(h.residual_wh)}</td><td>${fmt(h.draw_w, 0)}</td>`;
      historyBody.appendChild(tr);
    });

  const events = session.events || [];
  const eventList = $("ho-event-list");
  eventList.innerHTML = "";
  events
    .slice()
    .reverse()
    .forEach((e) => {
      const li = document.createElement("li");
      let text = "";
      switch (e.kind) {
        case "create":
          text = `开始演练，余电 ${fmt(e.residual_wh)} Wh`;
          break;
        case "advance":
          text = `推进到第 ${e.to} 分钟`;
          break;
        case "revise":
          text = `补录余电 ${fmt(e.residual_wh)} Wh：保留 ${e.preserved} 条，重排 ${e.rescheduled} 条`;
          break;
        case "complete":
          text = `全部设备停妥，剩余 ${fmt(e.residual_wh)} Wh`;
          break;
        case "handover_invite":
          text = `生成第 ${e.handover_no} 次交接链接`;
          break;
        case "handover":
          text =
            `第 ${e.handover_no} 次交接完成：第 ${e.minute} 分钟、第 ${e.step} 步，` +
            `${e.issued_count} 条已生效命令移交`;
          break;
        default:
          text = e.kind;
      }
      const step = e.step !== undefined ? ` · 第 ${e.step} 步` : "";
      li.innerHTML =
        `<span class="t">${e.at}m${step}</span> ${escapeHtml(text)}`;
      eventList.appendChild(li);
    });

  const last = events.filter((e) => e.kind === "handover").slice(-1)[0];
  $("ho-drill-note").innerHTML = last
    ? `交接记录：第 ${last.minute} 分钟、第 ${last.step} 步，` +
      `${last.issued_count} 条已生效命令移交；本页沿用同一场演练的排程与记录。`
    : "";

  $("ho-revise-input").value = fmt(session.residual_wh, 1);
  renderHandoverBox();
  const done = session.status === "complete";
  $("ho-step").disabled = HO.busy || !session.can_advance;
  $("ho-next").disabled = HO.busy || !session.can_advance;
  $("ho-finish").disabled = HO.busy || !session.can_advance || done;
  $("ho-revise").disabled = HO.busy || done;
}

// ---------------------------------------------------------------------------
// Network actions
// ---------------------------------------------------------------------------

async function loadPreview({ initial = false } = {}) {
  if (HO.busy || HO.accepted) return;
  const headers = {};
  if (!initial && HO.displayedVersion !== null) {
    headers["X-View-Version"] = String(HO.displayedVersion);
  }
  try {
    const view = await api(`/api/handovers/${encodeURIComponent(HO.token)}`, {
      headers,
    });
    if (HO.accepted) return;
    renderPreview(view);
  } catch (err) {
    handleLinkError(err);
  }
}

function handleLinkError(err) {
  stopPolling();
  HO.view = null;
  $("ho-accept").hidden = true;
  $("ho-badge").textContent = "链接不可用";
  $("ho-badge").className = "badge bad";
  if (err.status === 409 || err.status === 404) {
    $("ho-title").textContent = "该交接链接已不可用";
    $("ho-subtitle").textContent = err.message;
    $("ho-ack").textContent =
      "一个链接只能成功交接一次，请向原值班员索取新的链接。";
    $("ho-notice").hidden = true;
  } else {
    showError(err.message);
  }
}

async function accept() {
  if (HO.busy || HO.accepted || !HO.view) return;
  // If the drill moved after the successor looked, the first click is the
  // explicit re-confirmation of the newest state; the next click accepts it.
  if (HO.view.stale && HO.acknowledgedVersion !== HO.view.view_version) {
    HO.acknowledgedVersion = HO.view.view_version;
    const accept = $("ho-accept");
    accept.textContent = "重新确认接手";
    $("ho-ack").textContent =
      `已核对第 ${HO.view.now} 分钟的最新状态，请点击“重新确认接手”完成交接。`;
    return;
  }
  HO.busy = true;
  showError("");
  $("ho-accept").disabled = true;
  $("ho-accept").textContent = "正在接手…";
  try {
    const session = await api(
      `/api/handovers/${encodeURIComponent(HO.token)}/accept`,
      {
        method: "POST",
        body: JSON.stringify({ view_version: HO.displayedVersion }),
      },
    );
    HO.ownerToken = session.owner_token;
    HO.ownerEpoch = session.owner_epoch;
    persistOwnership();
    stopPolling();
    renderAccepted(session);
    // Keep the successor's page in sync with the outgoing operator's last
    // actions without ever touching the drill clock.
    HO.pollTimer = setInterval(() => refreshAccepted(), 4000);
  } catch (err) {
    if (err.status === 409 && err.payload && err.payload.state) {
      // The drill moved between view and confirmation: show the latest state
      // and make the successor explicitly confirm the new version.
      const state = err.payload.state;
      HO.acknowledgedVersion = null;
      renderPreview(state);
      const notice = $("ho-notice");
      notice.hidden = false;
      notice.innerHTML =
        "⚠ 演练状态在查看后已变化，下面是最新状态，请重新确认接手。";
      $("ho-ack").textContent = `当前第 ${state.now} 分钟（版本 ${state.view_version}），请核对后重新确认。`;
      $("ho-accept").textContent = "已核对最新状态，重新确认接手";
    } else if (err.status === 409 || err.status === 404) {
      handleLinkError(err);
    } else {
      showError(err.message);
    }
  } finally {
    HO.busy = false;
    if (!HO.accepted && HO.view) $("ho-accept").disabled = false;
  }
}

async function refreshAccepted() {
  if (!HO.session) return;
  try {
    const session = await api(`/api/drills/${HO.session.id}`);
    if (!HO.accepted) return;
    HO.session = session;
    if (session.read_only) {
      // This shift has handed the drill on again: stop controlling it and
      // make the page read-only, just like the very first operator page.
      HO.ownerToken = null;
      HO.ownerEpoch = null;
      HO.invite = null;
      $("ho-readonly-banner").hidden = false;
      $("ho-btn-handover").disabled = true;
      $("ho-step").disabled = true;
      $("ho-next").disabled = true;
      $("ho-finish").disabled = true;
      $("ho-revise").disabled = true;
      for (const id of ["ho-accept", "ho-refresh", "ho-btn-handover", "ho-btn-handover-copy"]) {
        if ($(id)) $(id).disabled = true;
      }
    } else {
      HO.ownerEpoch = session.owner_epoch;
    }
    renderDrill();
  } catch {
    /* transient poll failure: the next tick retries */
  }
}

async function advance(body) {
  if (HO.busy || !HO.session) return;
  HO.busy = true;
  showError("", "ho-drill-error");
  try {
    const session = await api(`/api/drills/${HO.session.id}/advance`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    HO.session = session;
    HO.ownerEpoch = session.owner_epoch;
    persistOwnership();
    renderDrill();
  } catch (err) {
    showError(err.message, "ho-drill-error");
  } finally {
    HO.busy = false;
    renderDrill();
  }
  await refreshAccepted();
}

async function revise() {
  if (HO.busy || !HO.session) return;
  const value = Number($("ho-revise-input").value);
  if (Number.isNaN(value) || value < 0) {
    showError("请输入有效的余电读数（Wh，≥ 0）", "ho-drill-error");
    return;
  }
  HO.busy = true;
  showError("", "ho-drill-error");
  try {
    const session = await api(`/api/drills/${HO.session.id}/revise`, {
      method: "POST",
      body: JSON.stringify({ residual_wh: value }),
    });
    HO.session = session;
    HO.ownerEpoch = session.owner_epoch;
    persistOwnership();
    renderDrill();
  } catch (err) {
    showError(err.message, "ho-drill-error");
  } finally {
    HO.busy = false;
    renderDrill();
  }
  await refreshAccepted();
}

function renderHandoverBox() {
  const session = HO.session;
  if (!session) return;
  const count = session.handover_count || 0;
  $("ho-handover-status").innerHTML = count
    ? `本场演练已完成 ${count} 次交接；再次交接后本页也会转为只读。`
    : "尚未交接。";
  const invite = HO.invite;
  const linkBox = $("ho-handover-link");
  if (invite) {
    linkBox.hidden = false;
    $("ho-handover-url").value = new URL(invite.path, window.location.origin).href;
    $("ho-handover-hint").textContent =
      `一次性链接：打开后由下一班先查看第 ${invite.created_minute} 分钟的状态，确认后生效。`;
  } else {
    linkBox.hidden = true;
  }
  $("ho-btn-handover").disabled = HO.busy || !session.can_advance;
}

async function issueHandover() {
  if (HO.busy || !HO.session) return;
  HO.busy = true;
  showError("", "ho-drill-error");
  try {
    const result = await api(`/api/drills/${HO.session.id}/handovers`, {
      method: "POST",
    });
    HO.invite = result;
    if (result.session) {
      HO.session = result.session;
      HO.ownerEpoch = result.session.owner_epoch;
      persistOwnership();
    }
    renderDrill();
  } catch (err) {
    showError(err.message, "ho-drill-error");
  } finally {
    HO.busy = false;
    renderDrill();
  }
}

async function copyHandoverLink() {
  const input = $("ho-handover-url");
  if (!input || !input.value) return;
  try {
    await navigator.clipboard.writeText(input.value);
    $("ho-handover-hint").textContent = "已复制，发给下一班打开即可。";
  } catch {
    input.select();
  }
}

function persistOwnership() {
  try {
    sessionStorage.setItem(
      storageKey(),
      JSON.stringify({
        ownerToken: HO.ownerToken,
        ownerEpoch: HO.ownerEpoch,
        sessionId: HO.session ? HO.session.id : null,
        handover: HO.session ? HO.session.handover : null,
      }),
    );
  } catch {
    /* private mode without storage: the page still works until reload */
  }
}

// A reload of an already-accepted link resumes the successor view rather than
// trying (and failing) to use the one-time link a second time.
async function resumeIfAccepted() {
  let saved = null;
  try {
    saved = JSON.parse(sessionStorage.getItem(storageKey()) || "null");
  } catch {
    saved = null;
  }
  if (!saved || !saved.ownerToken || !saved.sessionId) return false;
  try {
    const session = await api(`/api/drills/${saved.sessionId}`, {
      headers: {
        "X-Operator-Token": saved.ownerToken,
        "X-Owner-Epoch": String(saved.ownerEpoch),
      },
    });
    if (session.read_only) return false;
    HO.ownerToken = saved.ownerToken;
    HO.ownerEpoch = saved.ownerEpoch;
    HO.session = session;
    renderAccepted(session);
    HO.pollTimer = setInterval(() => refreshAccepted(), 4000);
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

function bind() {
  $("ho-accept").addEventListener("click", accept);
  $("ho-refresh").addEventListener("click", () => loadPreview());
  $("ho-step").addEventListener("click", () => advance({ minutes: 1 }));
  $("ho-next").addEventListener("click", () => advance({ to_next_event: true }));
  $("ho-finish").addEventListener("click", () => {
    if (!HO.session) return;
    const remaining = HO.session.plan.makespan - HO.session.now;
    advance({ minutes: Math.max(1, remaining) });
  });
  $("ho-revise").addEventListener("click", revise);
  $("ho-btn-handover").addEventListener("click", issueHandover);
  $("ho-btn-handover-copy").addEventListener("click", copyHandoverLink);
}

async function init() {
  bind();
  HO.token = tokenFromPath();
  if (!HO.token) {
    $("ho-title").textContent = "交接链接无效";
    $("ho-subtitle").textContent = "请使用原值班员生成的完整链接打开本页。";
    $("ho-badge").textContent = "链接无效";
    $("ho-badge").className = "badge bad";
    return;
  }
  if (await resumeIfAccepted()) return;
  await loadPreview({ initial: true });
  if (!HO.accepted) {
    HO.pollTimer = setInterval(() => loadPreview(), 5000);
  }
}

// Debug hook for the automated two-window walkthrough.
window.HO = HO;

document.addEventListener("DOMContentLoaded", init);
