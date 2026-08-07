"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "..");
const script = fs.readFileSync(path.join(root, "static", "research.js"), "utf8");
const context = { URL, window: { __VIRA_TEST__: true } };
vm.createContext(context);
vm.runInContext(script, context, { filename: "research.js" });
const hooks = context.window.__VIRA_RESEARCH_TESTS__;
assert.ok(hooks, "research test hooks were not installed");

assert.deepEqual(
  { ...hooks.relationNavigation({
    source_id: "copy", related_source_id: "root", other_source_id: "root",
    navigation_label: "View canonical source",
  }, "copy") },
  { id: "root", label: "View canonical source" },
);
assert.equal(hooks.relationNavigation({
  source_id: "same", related_source_id: "same", other_source_id: "same",
}, "same").id, "", "a malformed relation must not navigate to itself");
assert.equal(hooks.relationNavigation({
  source_id: "left", related_source_id: "right", other_source_id: "right",
}, "left").label, "View related source");

const staleSource = {
  canonical_url: "https://youtube.com/watch?v=canonical",
  original_url: "https://youtube.com/watch?v=canonical&email=person%40example.test",
};
const staleEvidence = {
  timestamp_seconds: 216,
  timestamped_original_url: "https://youtube.com/watch?v=repost&t=216s",
  original_url: "https://youtube.com/watch?v=repost",
};
assert.equal(hooks.evidenceSourceUrl(staleEvidence, staleSource),
  "https://youtube.com/watch?v=canonical&t=216s");
assert.equal(hooks.sourceExternalUrl({}, staleSource),
  "https://youtube.com/watch?v=canonical");
assert.equal(hooks.safeExternalUrl(
  "https://example.test/path?v=7&email=person%40example.test&utm_source=feed"),
"https://example.test/path?v=7");
assert.equal(hooks.safeExternalUrl("javascript:alert(1)"), "");

assert.deepEqual(
  { ...hooks.segmentPresentation({ locator: "Section: What we are looking for", text: "Copy" }) },
  {
    stamp: "",
    heading: "Section: What we are looking for",
    className: "research-segment-full",
  },
);
assert.deepEqual(
  { ...hooks.segmentPresentation({ timestamp_seconds: 209, text: "Quote" }) },
  { stamp: "3:29", heading: "", className: "research-segment-timed" },
);
assert.deepEqual(
  { ...hooks.segmentPresentation({ timestamp_start: "00:03:29", text: "Quote" }) },
  { stamp: "00:03:29", heading: "", className: "research-segment-timed" },
);
assert.deepEqual(
  { ...hooks.segmentPresentation({ locator: "repost 00:03:29", text: "Quote" }) },
  { stamp: "00:03:29", heading: "", className: "research-segment-timed" },
);

let focused = false;
const heading = { focus(options) {
  focused = options?.preventScroll === true;
} };
const inspector = {
  scrollTop: 600,
  querySelector(selector) {
    assert.equal(selector, ".research-inspector-title");
    return heading;
  },
};
assert.equal(hooks.focusInspectorHeading(inspector), true);
assert.equal(inspector.scrollTop, 0);
assert.equal(focused, true);
