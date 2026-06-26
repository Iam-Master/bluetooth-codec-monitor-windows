"use strict";
// Tests for CMUtils.isSafeColor / safeColor (CSS color allowlist).
// CommonJS require() of the UMD module; run with: node --test tests
const test = require("node:test");
const assert = require("node:assert");
const { isSafeColor, safeColor } = require("../utils.js");

test("accepts hex colors (#abc, #aabbcc, #aabbccdd)", () => {
  assert.strictEqual(isSafeColor("#abc"), true);
  assert.strictEqual(isSafeColor("#aabbcc"), true);
  assert.strictEqual(isSafeColor("#aabbccdd"), true);
});

test("accepts rgb(1,2,3)", () => {
  assert.strictEqual(isSafeColor("rgb(1,2,3)"), true);
});

test("accepts rgba(1,2,3,0.5)", () => {
  assert.strictEqual(isSafeColor("rgba(1,2,3,0.5)"), true);
});

test("accepts hsl(200,50%,50%)", () => {
  assert.strictEqual(isSafeColor("hsl(200,50%,50%)"), true);
});

test("accepts named color teal", () => {
  assert.strictEqual(isSafeColor("teal"), true);
});

test("rejects CSS-injection payload via url(javascript:)", () => {
  assert.strictEqual(
    isSafeColor("red; background:url(javascript:alert(1))"),
    false
  );
});

test("rejects markup/script-style payloads", () => {
  assert.strictEqual(isSafeColor('"><script>'), false);
  assert.strictEqual(isSafeColor("url(x)"), false);
  assert.strictEqual(isSafeColor("expression(1)"), false);
});

test("rejects non-string and empty string", () => {
  assert.strictEqual(isSafeColor(123), false);
  assert.strictEqual(isSafeColor(null), false);
  assert.strictEqual(isSafeColor(undefined), false);
  assert.strictEqual(isSafeColor(""), false);
});

test("rejects overly long string (> 32 chars)", () => {
  assert.strictEqual(isSafeColor("a".repeat(33)), false);
});

test("safeColor returns fallback for unsafe input and trimmed value for safe input", () => {
  // unsafe -> fallback
  assert.strictEqual(safeColor("url(x)", "#000"), "#000");
  assert.strictEqual(safeColor("expression(1)"), "#888"); // default fallback
  // safe -> trimmed value
  assert.strictEqual(safeColor("  teal  "), "teal");
  assert.strictEqual(safeColor("#aabbcc"), "#aabbcc");
});
