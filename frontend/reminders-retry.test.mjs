import assert from "node:assert/strict";
import test from "node:test";

class FakeHTMLElement {
  attachShadow() {
    this.shadowRoot = { querySelector: () => null, querySelectorAll: () => [] };
    return this.shadowRoot;
  }
  toggleAttribute() {}
}
const registry = new Map();
globalThis.HTMLElement = FakeHTMLElement;
globalThis.customElements = {
  define: (name, constructor) => registry.set(name, constructor),
  get: (name) => registry.get(name),
};

await import("../custom_components/reminders/frontend/reminders-panel.js");
const Panel = customElements.get("reminders-management-panel");

test("Retry now reschedules a failed one-time reminder immediately", async () => {
  const panel = new Panel();
  const calls = [];
  panel._call = async (command, payload) => { calls.push({ command, payload }); };
  panel._load = async () => { calls.push({ command: "reload" }); };
  const before = Date.now();
  await panel._retryFailed({ id: "failed-one-off" });
  const after = Date.now();
  assert.equal(calls[0].command, "snooze");
  assert.equal(calls[0].payload.reminder_id, "failed-one-off");
  const due = Date.parse(calls[0].payload.due);
  assert.ok(due >= before && due <= after);
  assert.equal(calls[1].command, "reload");
});

test("Retry now reports an error instead of silently ignoring it", async () => {
  const panel = new Panel();
  panel._call = async () => { throw new Error("service unavailable"); };
  panel._load = async () => { throw new Error("should not reload"); };
  panel._showError = (error) => { panel.reported = error.message; };
  await panel._retryFailed({ id: "failed-one-off" });
  assert.equal(panel.reported, "service unavailable");
});
