"use strict";
// Tests for CMUtils.trimHistory(arr, max, keep).
// CommonJS require() of the UMD module; run with: node --test tests
const test = require("node:test");
const assert = require("node:assert");
const { trimHistory } = require("../utils.js");

test("array under max is returned unchanged (same reference)", () => {
  const arr = [1, 2, 3];
  const out = trimHistory(arr, 5, 5);
  assert.strictEqual(out, arr);
  assert.deepStrictEqual(out, [1, 2, 3]);
});

test("array at exactly max is returned unchanged", () => {
  const arr = [1, 2, 3];
  const out = trimHistory(arr, 3, 2);
  assert.strictEqual(out, arr); // length (3) is not > max (3)
  assert.deepStrictEqual(out, [1, 2, 3]);
});

test("max+1 is trimmed to keep length", () => {
  const out = trimHistory([1, 2, 3, 4], 3, 3);
  assert.strictEqual(out.length, 3);
  assert.deepStrictEqual(out, [2, 3, 4]);
});

test("large array is trimmed down to keep length", () => {
  const arr = Array.from({ length: 5000 }, (_, i) => i + 1);
  const out = trimHistory(arr, 2200, 2200);
  assert.strictEqual(out.length, 2200);
  assert.strictEqual(out[out.length - 1], 5000);
  assert.strictEqual(out[0], 2801); // 5000 - 2200 + 1
});

test("non-array input returns []", () => {
  assert.deepStrictEqual(trimHistory(null, 5, 5), []);
  assert.deepStrictEqual(trimHistory(undefined, 5, 5), []);
  assert.deepStrictEqual(trimHistory("abc", 5, 5), []);
  assert.deepStrictEqual(trimHistory(42, 5, 5), []);
});

test("keep can differ from max (keep < max)", () => {
  const out = trimHistory([1, 2, 3, 4, 5], 4, 2);
  assert.deepStrictEqual(out, [4, 5]);
});

test("returns the last elements, order preserved", () => {
  const out = trimHistory([10, 20, 30, 40, 50], 3, 3);
  assert.deepStrictEqual(out, [30, 40, 50]);
});

test("empty array returns [] (unchanged)", () => {
  const arr = [];
  const out = trimHistory(arr, 5, 5);
  assert.ok(Array.isArray(out));
  assert.strictEqual(out, arr);
  assert.deepStrictEqual(out, []);
});

test("max=0 edge trims any non-empty array", () => {
  assert.deepStrictEqual(trimHistory([1, 2, 3], 0, 2), [2, 3]);
});

test("content correctness of retained slice", () => {
  const arr = ["a", "b", "c", "d", "e", "f"];
  const out = trimHistory(arr, 4, 3);
  assert.deepStrictEqual(out, ["d", "e", "f"]);
  assert.strictEqual(out.length, 3);
});
