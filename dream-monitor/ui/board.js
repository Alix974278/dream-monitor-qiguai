/* Pure catalogue helpers, also exercised by the offline Node tests. */
(function (root) {
  "use strict";
  const labels = { soon: "符合条件 · 等待开抢", open: "现在可报名", unknown: "资格待确认",
    ineligible: "当前不可报名", full: "已满员", closed: "报名已截止", joined: "已报名" };
  function status(it, now) {
    if (it.joined_by_me) return "joined";
    if ((it.sign_end && now >= it.sign_end) || it.registration_phase === "closed") return "closed";
    if (it.quota === 0 || it.state === "full") return "full";
    if (it.eligibility === "ineligible") return "ineligible";
    if (it.eligibility !== "eligible") return "unknown";
    if ((it.sign_start && now < it.sign_start) || (!it.sign_start && it.registration_phase === "soon")) return "soon";
    return it.can_join === true ? "open" : "unknown";
  }
  function pass(it, filter, now) {
    const st = status(it, now);
    if (filter === "all") return true;
    if (filter === "eligible") return st === "soon" || st === "open";
    return st === filter;
  }
  function group(it, now) {
    if (!Number.isFinite(it.sign_start) || it.sign_start <= 0) return 3;
    if (it.sign_end && now >= it.sign_end) return 2;
    if (it.sign_start > now) return 0;
    return it.registration_phase === "open" || status(it, now) === "open" ? 1 : 2;
  }
  function compare(a, b, now) {
    return group(a, now) - group(b, now) ||
      (a.sign_start || Infinity) - (b.sign_start || Infinity) ||
      String(a.aid).localeCompare(String(b.aid), "en");
  }
  function list(items, filter, now) {
    return items.filter(it => pass(it, filter, now)).sort((a, b) => compare(a, b, now));
  }
  function counts(items, now) {
    const out = { all: items.length, eligible: 0, soon: 0, open: 0, unknown: 0 };
    for (const it of items) {
      const st = status(it, now);
      if (st in out) out[st]++;
      if (st === "soon" || st === "open") out.eligible++;
    }
    return out;
  }
  function countdown(ts, now) {
    if (!ts) return "开抢时间待确认";
    const n = Math.ceil(ts - now);
    if (n <= 0) return "已到开抢时间";
    const days = Math.floor(n / 86400), hours = Math.floor(n % 86400 / 3600);
    const mins = Math.floor(n % 3600 / 60), secs = n % 60;
    return "距开抢 " + (days ? `${days}天 ${hours}小时` : hours ? `${hours}小时 ${mins}分` : `${mins}分 ${secs}秒`);
  }
  const api = { labels, status, pass, group, compare, list, counts, countdown };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.Board = api;
})(typeof window === "undefined" ? globalThis : window);
