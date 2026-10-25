import assert from "node:assert/strict";
import test from "node:test";

class FakeHTMLElement {
  attachShadow() { this.shadowRoot = {}; return this.shadowRoot; }
  toggleAttribute() {}
}
const registry = new Map();
globalThis.HTMLElement = FakeHTMLElement;
globalThis.customElements = {
  define: (name, ctor) => registry.set(name, ctor),
  get: (name) => registry.get(name),
};
await import("../custom_components/reminders/frontend/reminders-panel.js");
const Panel = customElements.get("reminders-management-panel");

function setup() {
  const panel = new Panel();
  const cancel = {};
  const confirm = { disabled: false };
  const dialog = {
    closeCount: 0,
    showModal() {},
    close() { this.closeCount++; },
    querySelector(selector) { return { ".cancel": cancel, ".confirm": confirm }[selector]; },
    set innerHTML(value) { this.markup = value; },
  };
  panel.shadowRoot = { querySelector: () => dialog };
  panel._formatDate = (value) => value;
  panel._load = async () => true;
  panel._showError = (error) => { panel.lastError = error; };
  panel._confirmSkip({ id: "series", due: "2026-10-26T10:00:00Z" });
  return { panel, dialog, confirm };
}

test("a rejected skip stays open, reports its failure and permits retry", async () => {
  const { panel, dialog, confirm } = setup();
  const failure = new Error("Could not save skip");
  panel._call = async () => { throw failure; };
  await confirm.onclick();
  assert.equal(dialog.closeCount, 0);
  assert.equal(confirm.disabled, false);
  assert.equal(panel.lastError, failure);
});

test("skip is committed once despite a failed post-action refresh", async () => {
  const { panel, dialog, confirm } = setup();
  let calls = 0;
  panel._call = async (name, payload) => {
    assert.equal(name, "skip_next");
    assert.equal(payload.reminder_id, "series");
    calls++;
  };
  panel._load = async () => { throw new Error("Temporary list outage"); };
  await confirm.onclick();
  assert.equal(calls, 1);
  assert.equal(dialog.closeCount, 1);
  assert.match(panel.lastError.message, /Temporary list outage/);
  assert.equal(confirm.disabled, false);
});

test("skip cannot be submitted twice while the first request is pending", async () => {
  const { panel, dialog, confirm } = setup();
  let resolve;
  let calls = 0;
  panel._call = async () => {
    calls++;
    await new Promise((done) => { resolve = done; });
  };
  const first = confirm.onclick();
  const second = confirm.onclick();
  assert.equal(calls, 1);
  resolve();
  await Promise.all([first, second]);
  assert.equal(calls, 1);
  assert.equal(dialog.closeCount, 1);
});
