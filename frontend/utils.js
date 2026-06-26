/*
 * Codec Monitor — shared frontend utilities (CMUtils)
 *
 * UMD module: in the browser it attaches to window.CMUtils (loaded via
 * <script src="utils.js"> before app.js); under Node it is exported via
 * module.exports so the node:test suite in ./tests can require() it.
 *
 * Exports:
 *   escapeHtml(s)        -> HTML-escaped string (defends innerHTML sinks)
 *   isSafeColor(c)       -> bool; true only for a strict CSS color allowlist
 *   safeColor(c, fb)     -> trimmed color if safe, else fallback (default #888)
 *   trimHistory(a,max,k) -> a.slice(-k) when a.length > max, else a ([] if !array)
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof window !== "undefined") { window.CMUtils = api; }
})(typeof self !== "undefined" ? self : this, function () {
  function escapeHtml(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function isSafeColor(c) {
    if (typeof c !== "string") return false;
    const s = c.trim();
    if (s.length === 0 || s.length > 32) return false;
    if (/^#[0-9a-fA-F]{3,8}$/.test(s)) return true;
    if (/^rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\)$/.test(s)) return true;
    if (/^rgba\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*(0|1|0?\.\d+)\s*\)$/.test(s)) return true;
    if (/^hsl\(\s*\d{1,3}\s*,\s*\d{1,3}%\s*,\s*\d{1,3}%\s*\)$/.test(s)) return true;
    if (/^[a-zA-Z]{1,20}$/.test(s)) return true;
    return false;
  }
  function safeColor(c, fallback) { return isSafeColor(c) ? c.trim() : (fallback || "#888"); }
  function trimHistory(arr, max, keep) { if (!Array.isArray(arr)) return []; return arr.length > max ? arr.slice(-keep) : arr; }
  return { escapeHtml, isSafeColor, safeColor, trimHistory };
});
