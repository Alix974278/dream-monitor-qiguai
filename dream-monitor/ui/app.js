/* 到梦空间活动看板：活动沿用原刷新流程；定时页另读取本机任务与校时状态。 */
const $ = (id) => document.getElementById(id);
const STATE_LABEL = { open: "可报名", soon: "即将开始", running: "进行中", full: "已满员", ended: "已结束" };

let S = { items: [], stats: {}, busy: false, last_sync: null, token: "" };
let filter = "eligible";
let filterOwner = null;
let serverOffset = 0;
const boardUpdates = new BoardUpdates.Gate();
let manualApplyRequested = false;
let manualApplyReady = false;
let refreshPromise = null;
let pullSerial = 0, lastPullApplied = 0;
function displayedBoard() { return boardUpdates.current || {items: [], now: boardNow()}; }
function renderUpdateNotice() {
  $("board-update-notice").hidden = !boardUpdates.pending || view !== "board";
}
function stageBoard() {
  const first = boardUpdates.receive(S, boardNow());
  if (first) { listSig = ""; renderList(); }
  if (manualApplyRequested && manualApplyReady && !S.busy && !S.enriching) {
    manualApplyRequested = false;
    manualApplyReady = false;
    applyBoardUpdate(false);
  }
  renderBoard();
  renderUpdateNotice();
}
function captureReadingPosition() {
  const scroller = document.querySelector(".scroll"), top = scroller.scrollTop;
  if (view !== "board" || top < 2) return {top, candidates: []};
  const edge = scroller.getBoundingClientRect().top;
  const rows = [...document.querySelectorAll("#stream .row")];
  // Ignore a tiny trailing fragment of the preceding row at the viewport edge.
  let index = rows.findIndex(row => {
    const rect = row.getBoundingClientRect();
    return rect.bottom > edge + Math.min(60, rect.height / 2);
  });
  if (index < 0) index = Math.max(0, rows.length - 1);
  const ordered = [...rows.slice(index), ...rows.slice(0, index).reverse()];
  return {top, candidates: ordered.map(row => ({aid: row.dataset.aid, offset: row.getBoundingClientRect().top - edge}))};
}
function restoreReadingPosition(saved) {
  const scroller = document.querySelector(".scroll"), edge = scroller.getBoundingClientRect().top;
  const rows = new Map([...document.querySelectorAll("#stream .row")].map(row => [row.dataset.aid, row]));
  const target = saved.candidates.find(candidate => rows.has(candidate.aid));
  if (target) scroller.scrollTop += rows.get(target.aid).getBoundingClientRect().top - edge - target.offset;
  else scroller.scrollTop = saved.top;
}
function applyBoardUpdate(showMessage = true) {
  const saved = captureReadingPosition();
  const focused = document.activeElement?.closest?.("#stream .row")?.dataset.aid;
  if (boardUpdates.apply()) {
    detailCache.clear();
    listSig = "";
    renderBoard(); renderList();
    if (focused) [...document.querySelectorAll("#stream .row")].find(row => row.dataset.aid === focused)?.focus({preventScroll:true});
    restoreReadingPosition(saved);
    if (showMessage) toast("列表已更新，已保留阅读位置");
  }
  renderUpdateNotice();
}
function boardNow() { return Date.now() / 1000 + serverOffset; }
function restoreFilter() {
  const owner = String(S.me?.uid || "guest");
  if (filterOwner === owner) return;
  const hadOwner = filterOwner !== null;
  filterOwner = owner;
  boardUpdates.reset(owner);
  manualApplyRequested = false;
  manualApplyReady = false;
  listSig = "";
  if (hadOwner) { closeDetail(); renderList(); }
  filter = "eligible";
  detailCache.clear();
  try {
    const saved = localStorage.getItem("dm-board-filter-v24:" + owner);
    if (["eligible", "soon", "open", "unknown", "all"].includes(saved)) filter = saved;
  } catch (_) {}
  syncFilterTabs();
}
function syncFilterTabs() {
  document.querySelectorAll("#tabs .tab").forEach(t => {
    const on = t.dataset.f === filter;
    t.classList.toggle("is-on", on);
    t.setAttribute("aria-selected", String(on));
  });
}
let detailCache = new Map();
let drawerAid = null;

/* ---------------- 数字滚动 ---------------- */
const REDUCED = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const shown = new WeakMap();
function rollNumber(el, to) {
  const from = shown.get(el) ?? 0;
  if (from === to) return;
  shown.set(el, to);
  if (REDUCED) { el.textContent = to; return; }
  const t0 = performance.now();
  const dur = 420;
  /* 用 performance.now() 而不是 rAF 回调的时间戳：后者可能早于 t0，
     相减得到负数，插值会算出 -3 这种值 */
  const step = () => {
    const p = Math.min(1, Math.max(0, (performance.now() - t0) / dur));
    const eased = 1 - Math.pow(1 - p, 3);
    el.textContent = p >= 1 ? to : Math.round(from + (to - from) * eased);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function stamp(ts) {
  if (!ts) return "尚未同步";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `同步于 ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function quotaText(it) {
  if (it.quota === -2) return "—";     // 详情没拿到，名额未知
  if (it.quota < 0) return "不限";
  return String(it.quota);
}

function untilText(ts) {
  if (!ts) return "";
  const diff = ts - Date.now() / 1000;
  if (diff <= 0) return "已截止";
  const h = diff / 3600;
  if (h >= 24) return `${Math.floor(h / 24)} 天后截止`;
  if (h >= 1) return `${Math.floor(h)} 小时后截止`;
  return `${Math.max(1, Math.floor(diff / 60))} 分钟后截止`;
}
/* 截止时间已按要求从列表移除，此函数仅保留给详情页备用 */

/* ---------------- 渲染 ---------------- */
function renderBoard() {
  const snapshot = displayedBoard();
  const c = Board.counts(snapshot.items, snapshot.now);
  rollNumber($("m-open"), c.eligible);
  rollNumber($("m-soon"), c.soon);
  rollNumber($("m-run"), c.open);
  const total = Math.max(1, c.all);
  $("g-open").style.width = `${c.open / total * 100}%`;
  $("g-soon").style.width = `${c.soon / total * 100}%`;
  $("g-run").style.width = `${c.unknown / total * 100}%`;
  ["g-open", "g-soon", "g-run"].forEach(id => $(id).style.transform = "scaleX(1)");
  for (const key of ["all", "eligible", "open", "soon", "unknown"]) $("c-" + key).textContent = c[key];
  $("board-refresh").disabled = !!S.busy;
  $("board-refresh").textContent = S.busy ? "更新中…" : "刷新活动与资格";
  $("stamp").textContent = stamp(S.last_sync);
  $("refresh").disabled = !!S.busy;
  $("refresh").textContent = S.busy ? "抓取中…" : "刷新活动";

  const auth = S.auth || {};
  const notice = $("notice");
  if (auth.stale) {
    notice.hidden = false;
    $("notice-text").textContent =
      `${auth.msg || "登录已失效"}。服务端是单点登录，重新登录会让手机端的到梦空间登录失效。`;
    $("notice-act").disabled = false;
  } else {
    notice.hidden = true;
  }
}

function passFilter(it) { return Board.pass(it, filter, displayedBoard().now); }

let listSig = "";
function renderList() {
  const stream = $("stream"), snapshot = displayedBoard(), now = snapshot.now;
  const items = Board.list(snapshot.items, filter, now);
  const counts = Board.counts(snapshot.items, now);
  const names = { eligible: "我能报名", soon: "即将开抢", open: "现在可报", unknown: "资格待确认", all: "全部活动" };
  $("board-hint").textContent = `${names[filter]} ${items.length} 个` +
    (filter === "eligible" ? " · 包含符合条件、尚未开放报名的活动" : "") +
    (counts.unknown && filter !== "unknown" ? ` · 另有 ${counts.unknown} 个资格待确认，可切换查看` : "");
  const sig = filter + "|" + BoardUpdates.signature(items, now);
  if (sig === listSig) { updateOpeningCountdowns(); return; }
  listSig = sig;
  if (!items.length) {
    const note = S.busy || S.enriching ? "正在读取当前账号的报名资格，请稍候…"
      : filter === "eligible" ? "暂无已确认符合条件的活动。可查看「资格待确认」或刷新活动与资格。"
      : "这个筛选下暂无活动。";
    stream.innerHTML = `<div class="empty">${note}</div>`;
    return;
  }
  let lastGroup = -1;
  stream.innerHTML = items.map((it, i) => {
    const state = Board.status(it, now), group = Board.group(it, now);
    const heading = group !== lastGroup ? `<h2 class="board-group">${["即将开放报名 · 最近开抢的在前", "已开放报名", "其他活动", "报名开始时间待确认"][group]}</h2>` : "";
    lastGroup = group;
    const hot = state === "open" && it.quota > 0 && it.quota <= 10;
    const tagCls = state === "soon" ? "tag--soon" : state === "open" ? "tag--open" : "tag--muted";
    const timing = it.sign_start ? fmtAutoTime(it.sign_start) : "时间待确认";
    const reason = ["ineligible", "unknown"].includes(state) ? it.eligibility_reason : "";
    return heading + `
      <article class="row" data-aid="${esc(it.aid)}" tabindex="0" role="button" aria-label="查看${esc(it.name || "活动")}详情">
        <span class="row-idx">${String(i + 1).padStart(2, "0")}</span>
        <div class="row-main">
          <h3 class="row-title">${esc(it.name || "（无标题）")}</h3>
          <div class="row-meta">
            <span class="tag ${tagCls}">${Board.labels[state]}</span>
            ${it.category ? `<span>${esc(it.category)}</span>` : ""}
            ${it.organizer ? `<span>${esc(it.organizer)}</span>` : ""}
          </div>
          <div class="row-opening"><span>报名开始</span><b>${esc(timing)}</b>
            ${it.sign_start > now ? `<em data-opening="${it.sign_start}">${Board.countdown(it.sign_start, now)}</em>` : ""}</div>
          <div class="row-secondary">${it.act_start ? `活动开始 ${esc(fmtAutoTime(it.act_start))}` : "活动时间待确认"}${it.address ? ` · ${esc(it.address)}` : ""}</div>
          ${reason ? `<div class="row-reason">${esc(reason)}</div>` : ""}
        </div>
        <div class="row-side"><b class="left-num ${hot ? "is-hot" : ""}">${quotaText(it)}</b><span class="left-cap">${it.quota === -2 ? "名额待确认" : it.quota < 0 ? "名额" : "剩余名额"}</span></div>
      </article>`;
  }).join("");
}
function updateOpeningCountdowns() {
  document.querySelectorAll("[data-opening]").forEach(el => {
    el.textContent = Board.countdown(Number(el.dataset.opening), boardNow());
  });
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

/* ---------------- 详情抽屉 ---------------- */
async function openDetail(aid) {
  drawerAid = String(aid);
  $("drawer").classList.add("is-on");
  $("drawer").setAttribute("aria-hidden", "false");
  $("scrim").classList.add("is-on");
  $("d-title").textContent = "读取中…";
  $("d-body").innerHTML = "";

  let d = detailCache.get(aid);
  if (d && (!d.eligibility_checked_at || boardNow() - d.eligibility_checked_at > 30 ||
      (d.sign_start && d.eligibility_checked_at < d.sign_start && boardNow() >= d.sign_start))) d = null;
  if (!d) {
    try {
      const r = await fetch(`/api/activity?aid=${encodeURIComponent(aid)}`);
      const j = await r.json();
      if (!r.ok || j.error) {
        throw new Error(j.error || ("HTTP " + r.status));
      }
      d = j.detail || j;
      detailCache.set(aid, d);
      const item = (S.items || []).find(it => String(it.aid) === String(aid));
      if (item) Object.assign(item, d, { name: d.activityName || item.name });
      stageBoard();
    } catch (e) {
      $("d-title").textContent = "读取失败";
      $("d-body").innerHTML =
        `<div class="drawer-note">这条活动的详情没取到：${esc(String(e && e.message || e))}</div>`
        + `<button class="btn btn--ghost" id="d-retry" type="button">重试</button>`;
      $("d-retry").addEventListener("click", () => {
        detailCache.delete(aid);
        openDetail(aid);
      });
      return;
    }
  }
  if (String(drawerAid) !== String(aid)) return;
  $("d-title").textContent = d.activityName || d.name || "—";

  const fmtDate = (v) => {
    if (v === undefined || v === null || v === "" || v === "—") return "—";
    const s = String(v);
    if (/^\d{10,13}$/.test(s)) {
      const t = Number(s);
      const dt = new Date(s.length === 13 ? t : t * 1000);
      if (!isNaN(dt)) {
        const p = (n) => String(n).padStart(2, "0");
        return `${dt.getFullYear()}.${p(dt.getMonth() + 1)}.${p(dt.getDate())} ${p(dt.getHours())}:${p(dt.getMinutes())}`;
      }
    }
    return s;
  };
  const startTxt = d.act_start ? fmtAutoTime(d.act_start) : (d.startdateDate ? fmtDate(d.startdateDate) : fmtDate(d.startdate));
  const endTxt = d.act_end ? fmtAutoTime(d.act_end) : (d.enddate || "—");
  const way = String(d.joinWayDesc || "").replace(/^报名制[:：]\s*/, "");
  // 审核制：待审核的人也占名额，剩余可能偏乐观
  const isReview = /审核/.test(String(d.joinWayDesc || "")) || String(d.joinWay || "2") !== "2";

  const rows = [
    ["状态", Board.labels[Board.status(d, boardNow())] || d.statusText || "—"],
    ["资格", d.eligibility_reason || "资格待确认"],
    ["开抢时间", d.sign_start ? fmtAutoTime(d.sign_start) + "（北京时间）" : "待确认"],
    ["名额", `${d.joinNum ?? "?"} / ${d.joinmaxnum ?? "?"}`],
    ["报名范围", d.joinrangeText || "—"],
    ["报名截止", d.sign_end ? fmtAutoTime(d.sign_end) + "（北京时间）" : "待确认"],
    ["活动", `${startTxt} 至 ${endTxt}（北京时间）`],
    ["地点", d.address || "—"],
    ["主办", d.schoolname || "—"],
    ["类别", d.catalog2name || "—"],
    ["级别", d.levelText || "—"],
  ];
  // 报名状态：详情里的 canJoin / cancel / signUpId 说了算
  const sid = String(d.signUpId ?? "0");
  // injoin 在未报名时也返回 true，只能看 signUpId
  const mine = !["", "0", "None", "null", "undefined"].includes(sid);
  const canJoin = Board.status(d, boardNow()) === "open" && !mine;
  const canCancel = d.can_cancel === true && mine;
  const scheduled = (S.auto || {})[aid];

  let action = "";
  if (mine) {
    action = `<div class="d-actions">
      <button class="btn btn--done" id="d-joined" type="button" disabled>已报名</button>
      <button class="btn btn--ghost" id="d-cancel" type="button" ${canCancel ? "" : "disabled"}>取消报名</button>
      <span class="d-hint">${canCancel ? "可以取消报名" : "当前不可取消"}</span>
    </div>`;
  } else if (scheduled && scheduled.status === "unknown") {
    action = `<div class="d-actions">
      <button class="btn btn--done" type="button" disabled>结果待核验</button>
      <span class="d-hint">可能已有报名请求发出。请先在到梦空间 App 核验个人报名记录，避免重复提交。</span>
    </div>`;
  } else if (scheduled && ["claimed", "submitting"].includes(scheduled.status)) {
    action = `<div class="d-actions">
      <button class="btn btn--done" type="button" disabled>${esc(autoStatusLabel(scheduled))}</button>
      <button class="btn btn--ghost" id="d-auto-off" type="button">停止后续尝试</button>
      <span class="d-hint">后台正在处理此任务；已经发出的请求不能通过取消撤回。</span>
    </div>`;
  } else if (canJoin) {
    action = `<div class="d-actions">
      <button class="btn btn--solid" id="d-join" type="button">立即报名</button>
      <span class="d-hint">报名成功后会出现在「我的活动」里</span>
    </div>`;
  } else if (d.registration_phase === "soon" && d.eligibility !== "ineligible" && d.quota !== 0) {
    const auto = (S.auto || {})[aid];
    if (auto) {
      action = `<div class="d-actions">
        <button class="btn btn--done" id="d-auto-on" type="button" disabled>已设定时</button>
        <button class="btn btn--ghost" id="d-auto-off" type="button">取消定时</button>
        <span class="d-hint">${esc(fmtAutoTime(auto.join_start))}（北京时间）由后台自动报名</span>
      </div>`;
    } else if (!signStart(d)) {
      action = `<div class="d-actions"><span class="d-hint">报名开始时间待确认，补全后可设定时报名。</span></div>`;
    } else {
      action = `<div class="d-actions">
        <button class="btn btn--solid" id="d-auto" type="button">定时报名</button>
        <span class="d-hint">${d.eligibility === "unknown" ? "资格待确认，请先在 App 核实条件。" : "符合报名条件。"}报名一开始（${esc(fmtAutoTime(signStart(d)))}，北京时间）由后台自动提交</span>
      </div>`;
    }
  } else {
    const status = Board.status(d, boardNow());
    const why = ["unknown", "ineligible"].includes(status)
      ? d.eligibility_reason || "资格待确认" : Board.labels[status] || "当前不可报名";
    action = `<div class="d-actions"><span class="d-hint">${esc(why)}</span></div>`;
  }

  $("d-body").innerHTML = rows.map(([k, v]) =>
    `<span class="dk">${k}</span><span class="dv">${esc(v || "—")}</span>`).join("")
    + (way ? `<div class="drawer-note"><b>报名制</b>　${esc(way)}</div>` : "")
    + (isReview ? `<div class="drawer-note"><b>审核制</b>　待审核的人也占名额，这里显示的剩余可能偏乐观，最终以 App 为准。</div>` : "")
    + action;

  const ab = $("d-auto");
  if (ab) ab.addEventListener("click", () => doAuto(aid, signStart(d)));
  const ao = $("d-auto-off");
  if (ao) ao.addEventListener("click", () => doAutoCancel(aid));
  const jb = $("d-join");
  if (jb) jb.addEventListener("click", () => doSignup(aid));
  const cb = $("d-cancel");
  if (cb) cb.addEventListener("click", () => doCancel(aid));
}


/* ---------------- 定时报名 ---------------- */
const AUTO_TIME = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const AUTO_TIME_MS = new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", fractionalSecondDigits: 3, hourCycle: "h23" });

function timingNumber(value) {
  if (!["number", "string"].includes(typeof value) || typeof value === "string" && !value.trim()) return null;
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

function timingEpoch(value) {
  const result = timingNumber(value);
  return result !== null && result > 0 ? result > 1e11 ? result / 1000 : result : null;
}

function fmtAutoTime(value, milliseconds = false) {
  const seconds = timingEpoch(value);
  return seconds === null ? "时间待核对" : (milliseconds ? AUTO_TIME_MS : AUTO_TIME).format(new Date(seconds * 1000));
}

function fmtTimingMs(value, signed = false, uncertainty = false) {
  const number = timingNumber(value);
  if (number === null || !signed && number < 0) return "—";
  return `${uncertainty ? "±" : signed && number > 0 ? "+" : ""}${number.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ms`;
}

function renderClock(clock) {
  const data = clock && typeof clock === "object" ? clock : {};
  const labels = {
    ready: data.ready === true ? ["自动校时已就绪", "ready"] : ["校时状态待确认", "unknown"],
    syncing: ["正在自动校时", "active"],
    unsynced: ["自动校时尚未完成", "unknown"],
    degraded: ["自动校时降级", "warning"],
    failed: ["本次校时未完成", "warning"],
    stale: ["自动校时待更新", "warning"],
    deferred: ["自动校时已暂缓", "warning"]
  };
  const [label, tone] = labels[data.status] || ["校时状态待更新", "unknown"];
  $("clock-status").textContent = label;
  $("clock-status").className = `clock-pill is-${tone}`;
  $("auto-clock-status").textContent = label;
  $("auto-clock-status").className = `tag clock-tag is-${tone}`;
  $("auto-clock-message").textContent = typeof data.message === "string" && data.message
    ? data.message : data.ready === true ? "后台报告当前时间基准可用，定时提交由后台执行。" : "尚未取得明确的自动校时结果；仍可按原流程设定定时报名。";
  $("auto-clock-now").textContent = timingEpoch(data.now) === null ? "—" : fmtAutoTime(data.now);
  $("auto-clock-offset").textContent = fmtTimingMs(data.offset_ms, true);
  $("auto-clock-uncertainty").textContent = fmtTimingMs(data.uncertainty_ms, false, true);
  $("auto-clock-rtt").textContent = fmtTimingMs(data.rtt_ms);
  $("auto-clock-synced").textContent = timingEpoch(data.last_sync_at) === null ? "尚未成功同步" : fmtAutoTime(data.last_sync_at);
  const names = { aliyun: "阿里云", ali: "阿里云", tencent: "腾讯云" };
  const sources = Array.isArray(data.sources) ? data.sources.filter(item => item && typeof item === "object") : [];
  $("auto-clock-sources").replaceChildren();
  sources.forEach(source => {
    const provider = typeof source.provider === "string" ? source.provider : "";
    const host = typeof source.host === "string" ? source.host : "";
    if (!provider && !host) return;
    const line = document.createElement("span"); line.className = "auto-clock-source";
    line.textContent = [names[provider.toLowerCase()] || provider, host].filter(Boolean).join(" · ");
    $("auto-clock-sources").append(line);
  });
  if (!$("auto-clock-sources").childElementCount) $("auto-clock-sources").textContent = "暂无已采用来源";
  $("auto-clock-lock").hidden = data.locked !== true;
}

/* 优先使用后台归一化的报名时间；旧格式回退按北京时间解析并保留秒。 */
function signStart(d) {
  if (!d) return 0;
  const normalized = timingEpoch(d.sign_start);
  if (normalized !== null) return normalized;
  const raw = d.joinstartdate ?? d.joinStartTime ?? d.signupStartTime ?? d.applyStartTime;
  if (raw !== undefined && raw !== null && /^\d{10,13}$/.test(String(raw))) {
    const t = Number(raw);
    return t > 1e11 ? t / 1000 : t;
  }
  const txt = String(d.joindate || "");
  const m = txt.match(/(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?/);
  if (m) {
    return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4] - 8, +m[5], +(m[6] || 0)) / 1000;
  }
  return 0;
}

const AUTO_LABEL = {
  pending: "等待中", retry: "尝试中", success: "已报名", failed: "未成功", cancelled: "已取消",
  claimed: "任务已接管", submitting: "正在提交", unknown: "结果待核验",
};
const AUTO_CANCELLABLE = new Set(["pending", "retry", "claimed", "submitting"]);

function autoStatusLabel(task) {
  const timing = task.timing && typeof task.timing === "object" ? task.timing : null;
  if (task.status === "submitting") {
    if (!timing) return "后台正在处理";
    if (timingEpoch(timing.actual_send_at) !== null || timingNumber(timing.sent_count) > 0) return "正在提交";
    if (["waiting", "ready"].includes(timing.phase)) return "等待到点";
    if (timing.phase === "preparing") return "正在提前准备";
    return "正在处理 · 尚未发起";
  }
  return AUTO_LABEL[task.status] || task.status;
}

function unresolvedAuto(aid) {
  return ["claimed", "submitting", "unknown"].includes((S.auto || {})[aid]?.status);
}

function explainUnresolvedAuto(aid) {
  toast((S.auto || {})[aid]?.status === "unknown"
    ? "该任务的结果待核验，请先到到梦空间 App 查看个人报名记录，避免重复提交。"
    : "后台正在处理此活动的定时任务，请等待结果，不要重复提交。");
}

async function doAuto(aid, at) {
  if (unresolvedAuto(aid)) { explainUnresolvedAuto(aid); return; }
  const item = (S.items || []).find((i) => i.aid === aid) || {};
  const name = item.name || "这条活动";
  const ok = await ask({
    title: "设定时报名？",
    text: `「${name}」报名一开始（${fmtAutoTime(at)}，北京时间），后台会自动替你提交报名。需要你保持在本站登录状态，并保持主程序运行、电脑开机联网。`,
    ok: "设定时",
  });
  if (!ok) return;
  if (unresolvedAuto(aid)) { explainUnresolvedAuto(aid); return; }
  const btn = $("d-auto");
  if (btn) { btn.disabled = true; btn.textContent = "设置中…"; }
  try {
    const r = await fetch("/api/auto", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ aid, at: at || 0 }),
    });
    const j = await r.json();
    if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
    toast(j.msg || "已设定时报名");
    await pull();
    await openDetail(aid);
  } catch (e) {
    toast("设置失败：" + (e && e.message || e));
    if (btn) { btn.disabled = false; btn.textContent = "定时报名"; }
  }
}

async function doAutoCancel(aid) {
  const status = (S.auto || {})[aid]?.status;
  if (status === "unknown") { explainUnresolvedAuto(aid); return; }
  const handling = ["claimed", "submitting"].includes(status);
  const ok = await ask({ title: handling ? "停止后续尝试？" : "取消定时？", text: handling ? "任务已由后台接管。取消会停止尚未发起的任务和后续尝试；如果请求已经发出，则不能撤回，结果仍需核验。" : "取消后将停止尚未发起的任务。已经发出的请求不能撤回。", ok: handling ? "停止后续尝试" : "取消定时" });
  if (!ok) return;
  if ((S.auto || {})[aid]?.status === "unknown") { explainUnresolvedAuto(aid); return; }
  try {
    const r = await fetch("/api/auto/cancel", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ aid }),
    });
    const j = await r.json();
    if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
    toast(handling ? "已请求停止后续尝试，已发出的请求仍需核验结果。" : "已取消定时");
    await pull();
    await openDetail(aid);
    if (view === "auto") loadAuto();
  } catch (e) {
    toast("取消失败：" + (e && e.message || e));
  }
}

let autoPollTimer = null;
let autoLoading = null;
let autoListSignature = "";

function scheduleAutoPoll() {
  clearTimeout(autoPollTimer);
  if (view === "auto" && !document.hidden) autoPollTimer = setTimeout(loadAuto, 1000);
}

function autoTimingMarkup(it) {
  const timing = it.timing && typeof it.timing === "object" ? it.timing : {};
  const facts = [];
  if (timingEpoch(timing.prepared_at) !== null) facts.push(`准备完成：${fmtAutoTime(timing.prepared_at)}`);
  else if (timing.prefetched === true) facts.push("报名信息已提前准备");
  if (timingEpoch(timing.actual_send_at) !== null) facts.push(`本机发起：${fmtAutoTime(timing.actual_send_at, true)}`);
  else if (!(timingNumber(timing.sent_count) > 0) && (timingNumber(timing.sent_count) === 0 || ["waiting", "ready", "preparing", "submitting"].includes(timing.phase))) facts.push("尚未发起报名请求");
  else facts.push(["pending", "claimed"].includes(it.status) && !(timingNumber(timing.attempts) > 0) ? "尚未记录本机发起" : "本机发起时间未记录");
  if (timingNumber(timing.dispatch_lateness_ms) !== null) facts.push(`本机调度差：${fmtTimingMs(timing.dispatch_lateness_ms, true)}`);
  const attempts = timingNumber(timing.attempts);
  if (attempts !== null && attempts >= 0) facts.push(`已尝试 ${Math.floor(attempts)} 次`);
  const phases = { preparing: "正在提前准备报名信息", prepared: "报名信息已提前准备", waiting: "等待计划时间", ready: "等待计划时间", claimed: "后台已接管任务", submitting: "正在处理报名请求", confirming: "正在核验报名结果", unknown: "结果待核验" };
  const message = typeof timing.message === "string" && timing.message ? timing.message : phases[timing.phase] || "";
  return `<div class="auto-timing">${facts.map(text => `<span>${esc(text)}</span>`).join("")}</div>`
    + (message ? `<p class="auto-phase">${esc(message)}</p>` : "")
    + (it.status === "unknown" ? '<p class="auto-unknown">可能已有请求发出。请先到到梦空间 App 核验个人报名记录，避免重复提交。</p>' : "");
}

function renderAuto(items) {
  const box = $("auto-list");
  $("auto-note").textContent = items.length
    ? `共 ${items.length} 条定时任务 · 北京时间 · 后台执行，关闭网页不影响；请保持主程序运行、电脑开机联网`
    : "还没有定时任务。到「活动」里点开一条未开始的活动，选「定时报名」。";
  const signature = JSON.stringify(items);
  if (signature === autoListSignature) return;
  autoListSignature = signature;
  if (!items.length) { box.innerHTML = ""; return; }
  box.innerHTML = items.map((it, idx) => {
    const live = AUTO_CANCELLABLE.has(it.status);
    const handling = ["claimed", "submitting"].includes(it.status);
    return `<div class="row row--auto" data-auto-aid="${esc(it.aid)}">
      <span class="row-idx">${String(idx + 1).padStart(2, "0")}</span>
      <div class="row-main">
        <h3 class="row-title">${esc(it.name || it.aid)}</h3>
        <p class="row-sub">
          <span class="tag tag--auto ${live ? "is-live" : ""} ${it.status === "unknown" ? "is-unknown" : ""}">${esc(autoStatusLabel(it))}</span>
          <span class="row-cat">${esc(fmtAutoTime(it.join_start))} 计划提交</span>
          ${it.note ? `<span class="row-place">${esc(String(it.note).slice(0, 40))}</span>` : ""}
        </p>
        ${autoTimingMarkup(it)}
      </div>
      ${live ? `<button class="btn btn--ghost btn--sm" data-auto-off="${esc(it.aid)}" type="button">${handling ? "停止后续" : "取消"}</button>` : ""}
    </div>`;
  }).join("");
  box.querySelectorAll("[data-auto-off]").forEach((b) => {
    b.addEventListener("click", () => doAutoCancel(b.getAttribute("data-auto-off")));
  });
}

async function loadAuto() {
  if (autoLoading) return autoLoading;
  autoLoading = (async () => {
    try {
      const r = await fetch("/api/auto", { credentials: "same-origin", cache: "no-store" });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || ("HTTP " + r.status));
      const items = Array.isArray(j.items) ? j.items : [];
      const previousDrawerStatus = drawerAid ? (S.auto || {})[drawerAid]?.status : null;
      S.auto = Object.fromEntries(items.map(item => [String(item.aid), item]));
      if (j.clock && typeof j.clock === "object") S.clock = j.clock;
      renderClock(S.clock);
      renderAuto(items);
      if (drawerAid && detailCache.has(drawerAid) && $("drawer").classList.contains("is-on") && previousDrawerStatus !== S.auto[drawerAid]?.status) openDetail(drawerAid);
    } catch (e) {
      autoListSignature = "";
      $("auto-list").innerHTML = `<div class="empty">读取失败：${esc(String(e && e.message || e))}</div>`;
    } finally { autoLoading = null; scheduleAutoPoll(); }
  })();
  return autoLoading;
}

/* 轻提示 */
let toastTimer = null;
function toast(msg) {
  const el = $("toast");
  if (!el) return;
  el.textContent = msg;
  el.classList.add("is-on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("is-on"), 2600);
}

/* 报名 / 取消报名 */
async function doSignup(aid) {
  if (unresolvedAuto(aid)) { explainUnresolvedAuto(aid); return; }
  const name = (S.items.find((i) => i.aid === aid) || {}).name || "这条活动";
  const ok = await ask({
    title: "报名参加？",
    text: `将用你的到梦空间账号报名「${name}」。报名成功后可在同一个地方取消。`,
    ok: "确认报名",
  });
  if (!ok) return;
  if (unresolvedAuto(aid)) { explainUnresolvedAuto(aid); return; }
  const btn = $("d-join");
  if (btn) { btn.disabled = true; btn.textContent = "提交中…"; }
  try {
    const r = await fetch("/api/signup", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ aid }),
    });
    const j = await r.json();
    if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
    await afterSignupChange(aid, j.msg || "报名成功");
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "立即报名"; }
    await ask({ title: "报名没成功", text: String(e.message || e), ok: "知道了" });
  }
}

async function doCancel(aid) {
  const name = (S.items.find((i) => i.aid === aid) || {}).name || "这条活动";
  const ok = await ask({
    title: "取消报名？",
    text: `将从「${name}」中退出报名。部分学校对取消次数有限制。`,
    ok: "取消报名",
  });
  if (!ok) return;
  const btn = $("d-cancel");
  if (btn) { btn.disabled = true; btn.textContent = "处理中…"; }
  try {
    const r = await fetch("/api/cancel", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ aid }),
    });
    const j = await r.json();
    if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
    await afterSignupChange(aid, j.msg || "已取消报名");
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "取消报名"; }
    await ask({ title: "取消失败", text: String(e.message || e), ok: "知道了" });
  }
}

async function afterSignupChange(aid, msg) {
  detailCache.delete(aid);
  await pull();                 // 列表里的「已报名」标记
  await openDetail(aid);        // 抽屉里的按钮状态
  if (view === "mine") await loadMine();
  toast(msg);
}

function closeDetail() {
  drawerAid = null;
  $("drawer").classList.remove("is-on");
  $("drawer").setAttribute("aria-hidden", "true");
  $("scrim").classList.remove("is-on");
}

/* ---------------- 视图切换 ---------------- */
let view = "board";

const VIEW_TITLE = { board: "活动", mine: "我的活动", auto: "定时报名", score: "积分" };

function setView(name, push = true) {
  view = ["score", "mine", "auto"].includes(name) ? name : "board";
  $("pageTitle").textContent = VIEW_TITLE[view];
  /* 筛选只属于活动页：我的活动 / 积分 不显示 全部·可报名·未开始 */
  $("tabs").hidden = view !== "board";
  $("v-board").hidden = view !== "board";
  $("v-score").hidden = view !== "score";
  $("v-mine").hidden = view !== "mine";
  $("v-auto").hidden = view !== "auto";
  document.querySelectorAll(".rail-item").forEach((b) =>
    b.classList.toggle("is-on", b.dataset.v === view));
  if (push) {
    const h = view === "score" ? "#score"
      : (view === "mine" ? "#mine" : (view === "auto" ? "#auto" : "#board"));
    if (location.hash !== h) history.replaceState(null, "", h);
  }
  if (view === "score") loadScore();
  if (view !== "auto") clearTimeout(autoPollTimer);
  if (view === "auto") loadAuto();
  if (view === "mine") loadMine();
  renderUpdateNotice();
}

/* ---------------- 我的活动 ---------------- */
let mineState = { items: [], at: 0 };

async function loadMine() {
  const list = $("mine-list");
  try {
    const r = await fetch("/api/mine", { credentials: "same-origin" });
    if (r.status === 401) { location.replace("/login.html"); return; }
    const j = await r.json();
    mineState = { items: j.items || [], at: j.at || 0 };
  } catch (e) {
    list.innerHTML = `<p class="empty">读取失败：${esc(String(e.message || e))}</p>`;
    return;
  }
  renderMine();
}

function renderMine() {
  const list = $("mine-list");
  const items = mineState.items || [];
  $("mine-who").textContent = items.length
    ? `已报名 ${items.length} 个活动` + (mineState.at ? ` · 读取于 ${fmtClock(mineState.at)}` : "")
    : "";
  if (!items.length) {
    list.innerHTML = `<p class="empty">还没有报名任何活动。去「活动」里挑一个，报名后这里就会出现。</p>`;
    return;
  }
  list.innerHTML = items.map((it, i) => `
    <article class="m-row" data-aid="${esc(it.aid)}" tabindex="0">
      <span class="m-idx">${String(i + 1).padStart(2, "0")}</span>
      <div class="m-main">
        <h3 class="m-title">${esc(it.name)}</h3>
        <div class="m-meta">
          <span class="tag tag--mine">已报名</span>
          <span>${esc(it.category || "—")}</span>
          <span class="m-time">${esc(it.time || "")}</span>
        </div>
      </div>
      <span class="m-state s-${esc(it.state)}">${esc(it.status_text || "")}</span>
    </article>`).join("");
  list.querySelectorAll(".m-row").forEach((el) => {
    const go = () => openDetail(el.dataset.aid);
    el.addEventListener("click", go);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  });
}

function fmtClock(ts) {
  const d = new Date((ts || 0) * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}.${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

$("rail").addEventListener("click", (e) => {
  const b = e.target.closest(".rail-item");
  if (b) setView(b.dataset.v);
});
$("clock-status").addEventListener("click", () => setView("auto"));

/* ---------------- 积分 ---------------- */
let scTimer = null;

function fmtTs(sec) {
  if (!sec) return "";
  const d = new Date(sec * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}.${p(d.getMonth() + 1)}.${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderScore(p) {
  const who = [p.name, p.college].filter(Boolean).join(" · ");
  $("sc-who").textContent = who;
  const s = p.score;
  if (s) {
    const v = Number(s.total);
    $("sc-score").textContent = Number.isFinite(v) ? String(Math.round(v)) : (s.text || "—");
    $("sc-cap").textContent =
      `数据来源：${s.source || p.score_source}` + (s.at ? ` · 读取于 ${fmtTs(s.at)}` : "");
  } else {
    $("sc-score").textContent = "…";
    $("sc-cap").textContent = p.score_scanning ? "正在读取（约 30 秒）…" : "还没有数据，点「重新统计」";
  }
}

async function loadScore() {
  try {
    const r = await fetch("/api/profile");
    if (r.status === 401) { location.replace("/login.html?again=1"); return; }
    const p = await r.json();
    renderScore(p);
    if (p.score_scanning) {
      clearTimeout(scTimer);
      scTimer = setTimeout(loadScore, 3000);
    }
  } catch (e) {
    $("sc-cap").textContent = `读取失败：${e}`;
  }
}

$("sc-logout").addEventListener("click", logout);
$("sc-rescan").addEventListener("click", async () => {
  $("sc-rescan").disabled = true;
  await fetch("/api/score", { method: "POST" });
  $("sc-cap").textContent = "正在读取（约 30 秒）…";
  setTimeout(() => { $("sc-rescan").disabled = false; }, 2000);
  loadScore();
});

/* 通用确认框：返回 Promise<boolean> */
let confirmResolve = null;
function ask(opts) {
  const d = $("confirm");
  $("confirm-title").textContent = opts.title || "确认操作？";
  $("confirm-text").textContent = opts.text || "";
  $("confirm-yes").textContent = opts.ok || "确认";
  if (!d.open) d.showModal();
  return new Promise((resolve) => { confirmResolve = resolve; });
}
function closeConfirm(v) {
  const d = $("confirm");
  if (d.open) d.close();
  if (confirmResolve) { confirmResolve(v); confirmResolve = null; }
}
$("confirm-yes").addEventListener("click", () => closeConfirm(true));
$("confirm-no").addEventListener("click", () => closeConfirm(false));
$("confirm").addEventListener("cancel", (e) => { e.preventDefault(); closeConfirm(false); });
$("confirm").addEventListener("click", (e) => {
  if (e.target === $("confirm")) closeConfirm(false);   // 点空白处取消
});

async function logout() {
  toggleMenu(false);
  const ok = await ask({
    title: "退出登录？",
    text: "退出后需要重新输入到梦空间账号和密码才能进来，看板数据保留在服务器上。",
    ok: "退出登录",
  });
  if (!ok) return;
  await fetch("/api/logout", { method: "POST" });
  location.replace("/login.html");
}

/* ---------------- 抓取进度 ---------------- */
function renderProgress(st) {
  const p = st.progress || {};
  const bar = $("pbar"), fill = $("pbar-fill"), txt = $("pbar-txt");
  if (boardUpdates.current || !st.busy || !p.phase) {
    if (!bar.hidden) {
      bar.hidden = true;
      fill.style.transform = "scaleX(0)";
    }
    return;
  }
  bar.hidden = false;
  const total = Number(p.total) || 0;
  const done = Number(p.done) || 0;
  const part = total ? Math.min(1, done / total) : 0;
  const overall = p.phase === "pages" ? part * 0.5 : 0.5 + part * 0.5;
  fill.style.transform = "scaleX(" + overall.toFixed(3) + ")";
  txt.textContent = p.phase === "pages"
    ? "抓取 " + done + " / " + total + " 页"
    : "补详情 " + done + " / " + total + " 条";
  bar.setAttribute("aria-valuenow", String(Math.round(overall * 100)));
}

/* ---------------- 数据 ---------------- */
async function pull() {
  const serial = ++pullSerial;
  const r = await fetch("/api/state");
  if (r.status === 401) { location.replace("/login.html"); return; }
  if (!r.ok) throw new Error("读取后台状态失败");
  const next = await r.json();
  if (serial < lastPullApplied) return;
  lastPullApplied = serial;
  S = next;
  if (Number.isFinite(S.server_now)) serverOffset = S.server_now - Date.now() / 1000;
  restoreFilter();
  if (S.mine_warming && !window.__mineRetry) {
    window.__mineRetry = setTimeout(() => { window.__mineRetry = null; pull().catch(() => {}); }, 5000);
  }
  if (S.me) {
    const who = S.me.name || S.me.account || "";
    const nm = $("me-name");
    if (nm) nm.textContent = S.me.name || "";
    const school = S.me.school || "";
    $("brand-sub").innerHTML =
      `${school ? esc(school) + " · " : ""}活动看板 <b class="brand-by">by 奇怪同学</b>`;
    document.title = `${who || "到梦空间"} · 活动看板`;
    setAvatar(S.me);
  }
  stageBoard();
  renderClock(S.clock);
  renderProgress(S);
  autoRefreshIfStale();
}

/* 后台按需获取新数据；已经展示的列表只在用户主动更新时替换。 */
let lastBoardRefresh = 0;
function autoRefreshIfStale() {
  if (S.busy || Date.now() - lastBoardRefresh < 120000) return;
  const criticalTask = Object.values(S.auto || {}).some(job =>
    ["pending", "retry", "claimed", "submitting"].includes(job.status) &&
    Math.abs(Number(job.join_start) - boardNow()) < 60);
  if (criticalTask) return; // 临近既有定时任务，不为刷新列表额外占用平台请求。
  const age = S.stale_secs;
  const crossedOpening = (S.items || []).some(it => it.sign_start &&
    it.eligibility_checked_at < it.sign_start && boardNow() >= it.sign_start &&
    (!it.sign_end || boardNow() < it.sign_end));
  if (age == null || age > 300 || crossedOpening) {
    lastBoardRefresh = Date.now();
    refresh({background:true}).catch(() => { /* 保留当前列表，下轮自动重试。 */ });
  }
}

async function refresh({background = false} = {}) {
  if (!background) { manualApplyRequested = true; manualApplyReady = false; toggleMenu(false); }
  if (refreshPromise) return refreshPromise;
  lastBoardRefresh = Date.now();
  refreshPromise = (async () => {
    try {
      const response = await fetch("/api/refresh", {method:"POST"});
      if (!response.ok) throw new Error("刷新请求失败");
      for (let i = 0; i < 120; i++) {
        await new Promise(resolve => setTimeout(resolve, 250));
        await pull();
        manualApplyReady = true;
        stageBoard();
        if (!S.busy && !S.enriching && S.last_sync) break;
      }
    } catch (error) {
      if (manualApplyRequested) toast("刷新失败，当前列表已保留，请稍后重试");
      manualApplyRequested = false;
      manualApplyReady = false;
      throw error;
    } finally {
      refreshPromise = null;
      renderBoard();
    }
  })();
  return refreshPromise;
}

/* ---------------- 事件 ---------------- */
$("tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  filter = btn.dataset.f;
  syncFilterTabs();
  try { localStorage.setItem("dm-board-filter-v24:" + filterOwner, filter); } catch (_) {}
  renderList();
});

$("stream").addEventListener("click", (e) => {
  const row = e.target.closest(".row");
  if (row) openDetail(row.dataset.aid);
});

$("drawer-close").addEventListener("click", closeDetail);
$("scrim").addEventListener("click", closeDetail);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeDetail();
});

$("refresh").addEventListener("click", () => refresh().catch(() => {}));
$("board-refresh").addEventListener("click", () => refresh().catch(() => {}));
$("board-apply").addEventListener("click", () => applyBoardUpdate());
$("stream").addEventListener("keydown", e => {
  if (e.key === "Enter" || e.key === " ") {
    const row = e.target.closest(".row");
    if (row) { e.preventDefault(); openDetail(row.dataset.aid); }
  }
});
setInterval(() => {
  if (view !== "board" || document.hidden) return;
  updateOpeningCountdowns();
  // Opening/deadline transitions create a reminder, never reorder the snapshot.
  stageBoard();
}, 1000);

function doRelogin() {
  const acc = (S.me && S.me.account) || "";
  const ok = window.confirm(
    "登录已失效，需要重新登录。\n\n" +
    "服务端是单点登录：重新登录后，手机端「到梦空间」上这个账号的登录会立刻失效，\n" +
    "需要重新登录才能继续用手机 App。\n\n" +
    "确定去登录页重新输入密码吗？"
  );
  if (!ok) return;
  const q = new URLSearchParams({ reauth: "1" });
  if (acc) q.set("account", acc);
  location.assign(`/login.html?${q.toString()}`);
}

$("notice-act").addEventListener("click", doRelogin);

/* ---------------- 头像菜单 ---------------- */
function toggleMenu(on) {
  const m = $("avatar-menu");
  const open = on ?? m.hidden;
  m.hidden = !open;
  $("avatar-btn").setAttribute("aria-expanded", String(open));
}

$("avatar-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  toggleMenu();
});
$("menu-logout").addEventListener("click", logout);
document.addEventListener("click", (e) => {
  if (!$("avatar-menu").hidden && !e.target.closest(".avatar-wrap")) toggleMenu(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("avatar-menu").hidden) toggleMenu(false);
});

function setAvatar(me) {
  /* 用背景图而不是 <img>：远端头像挂了也不会出现破图图标 */
  const btn = $("avatar-btn");
  const fb = $("avatar-fb");
  const img = $("avatar-img");
  if (img) img.hidden = true;
  const url = me && me.avatar;
  fb.textContent = (me && me.name ? me.name[0] : "—");
  fb.hidden = false;
  btn.style.backgroundImage = "";
  if (!url) return;
  const probe = new Image();
  probe.onload = () => {
    btn.style.backgroundImage = `url("${url}")`;
    fb.hidden = true;
  };
  probe.src = url;
}


pull().catch(() => {});
setView(location.hash === "#score" ? "score" : (location.hash === "#mine" ? "mine" : "board"), false);

// 支持 #aid=xxx 直接打开详情
window.addEventListener("hashchange", () => {
  if (["#score", "#board", "#mine"].includes(location.hash)) {
    setView(location.hash.slice(1), false);
    return;
  }
  const m = location.hash.match(/^#aid=(.+)$/);
  if (m) openDetail(m[1]);
});
if (/^#aid=/.test(location.hash)) openDetail(location.hash.slice(5));
if (location.hash === "#auto") setView("auto", false);

/* 平时 15 秒同步一次；后端还在补详情（enriching）时改成 0.9 秒，
   这样名额/地点是一格一格填上来的，不用干等。 */
let pollTimer = null;
function schedulePoll() {
  const hasData = !!(S.items && S.items.length);
  const wait = S.busy ? 400 : (S.enriching ? 900 : (hasData ? 15000 : 800));
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try { await pull(); } catch (_) { /* 下轮重试，保持阅读内容。 */ }
    finally { schedulePoll(); }
  }, wait);
}
schedulePoll();

/* 只更新本机定时状态，不加速活动同步，也不由浏览器发送定时报名。 */
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(autoPollTimer);
  else if (view === "auto") loadAuto();
});
