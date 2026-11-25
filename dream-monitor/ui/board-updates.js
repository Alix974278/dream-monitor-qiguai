/* Keep the displayed catalogue separate from live scheduler/account state. */
(function (root) {
  "use strict";
  const B = typeof module !== "undefined" && module.exports ? require("./board.js") : root.Board;
  const fields = ["aid", "name", "quota", "category", "organizer", "address",
    "sign_start", "sign_end", "act_start", "eligibility_reason", "joined_by_me"];
  function signature(items, now) {
    return JSON.stringify(items.map(it => [
      ...fields.map(key => it[key] ?? null), B.status(it, now), B.group(it, now),
    ]).sort((a, b) => String(a[0]).localeCompare(String(b[0]), "en")));
  }
  class Gate {
    constructor() { this.reset(null); }
    reset(owner) { this.owner = owner; this.current = null; this.pending = null; }
    receive(state, now) {
      const owner = String(state.me?.uid || "guest");
      if (owner !== this.owner) this.reset(owner);
      // Do not publish the intermediate list while permissions are incomplete.
      if (state.busy || state.enriching || !Array.isArray(state.items)) return false;
      if (!this.current && !state.last_sync && !state.items.length) return false;
      const sig = signature(state.items, now);
      if (this.current && sig === this.current.signature) { this.pending = null; return false; }
      if (this.pending && sig === this.pending.signature) { this.pending.now = now; return false; }
      const snapshot = {items: JSON.parse(JSON.stringify(state.items)), now,
        synced: state.last_sync, signature: sig};
      if (!this.current) { this.current = snapshot; return true; }
      this.pending = snapshot.signature === this.current.signature ? null : snapshot;
      return false;
    }
    apply() {
      if (!this.pending) return false;
      this.current = this.pending; this.pending = null; return true;
    }
  }
  const api = {Gate, signature};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.BoardUpdates = api;
})(typeof window === "undefined" ? globalThis : window);
