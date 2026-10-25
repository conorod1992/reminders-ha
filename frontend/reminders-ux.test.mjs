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
const Panel = registry.get("reminders-management-panel");

test("dirty-form snapshot tracks only editable state and native rules", () => {
  const panel = new Panel();
  const fields = [
    { name: "title", value: "Take bins out", type: "text" },
    { name: "manual", type: "checkbox", checked: false },
    { name: "internal_id", type: "hidden", value: "token" },
    { name: "disabled", type: "text", value: "ignored", disabled: true },
  ];
  const form = { querySelectorAll: () => fields };
  const original = panel._formSnapshot(form);
  fields[2].value = "changed";
  fields[3].value = "changed";
  assert.equal(panel._formSnapshot(form), original);
  fields[1].checked = true;
  assert.notEqual(panel._formSnapshot(form), original);
  fields[1].checked = false;
  form._nativeRules = { activation: [{ trigger: "state", entity_id: "light.room" }] };
  const nativeSnapshot = panel._formSnapshot(form);
  form._nativeRules.activation.push({ trigger: "event", event_type: "changed" });
  assert.notEqual(panel._formSnapshot(form), nativeSnapshot);
});

function makeSaveFixture() {
  const panel = new Panel();
  const errorHost = {
    textContent: "",
    classList: { add() {}, remove() {} },
    scrollIntoView() {},
  };
  const save = { disabled: false };
  const form = {
    _saving: false,
    elements: {
      activation_type: { value: "time" },
      title: { value: "Get bins" },
      message: { value: "" },
      acknowledgement_policy: { value: "default" },
      quiet_hours_policy: { value: "respect" },
      delivery_mode: { value: "default" },
      allow_manual_completion: { checked: false },
      expires_after_preset: { value: "" },
      deliver_when_enabled: { checked: false },
      complete_when_enabled: { checked: false },
      escalation_enabled: { checked: false },
      repeat: { checked: false },
      date: { value: "2026-12-01" },
      time: { value: "12:00" },
    },
    querySelector: (selector) => ({
      "[type=submit]": save,
      ".form-error": errorHost,
    })[selector],
    querySelectorAll: () => [],
  };
  const dialog = { closeCount: 0, close() { this.closeCount++; } };
  return { panel, form, dialog, save, errorHost };
}

test("one Save mutation is sent when a user double-submits", async () => {
  const { panel, form, dialog, save } = makeSaveFixture();
  let resolve;
  let requests = 0;
  panel._call = async () => {
    requests++;
    await new Promise((done) => { resolve = done; });
  };
  panel._load = async () => true;
  const first = panel._saveReminder(dialog, form, null);
  const second = panel._saveReminder(dialog, form, null);
  assert.equal(requests, 1);
  assert.equal(save.disabled, true);
  resolve();
  await Promise.all([first, second]);
  assert.equal(requests, 1);
  assert.equal(dialog.closeCount, 1);
  assert.equal(save.disabled, false);
});

test("a committed Save stays successful when list refresh fails", async () => {
  const { panel, form, dialog, errorHost } = makeSaveFixture();
  let mutations = 0;
  panel._call = async () => { mutations++; };
  panel._load = async () => { throw new Error("Connection temporarily lost"); };
  panel._showError = (error) => { panel.lastError = error; };
  await panel._saveReminder(dialog, form, null);
  assert.equal(mutations, 1);
  assert.equal(dialog.closeCount, 1);
  assert.equal(errorHost.textContent, "");
  assert.match(panel.lastError.message, /Connection temporarily lost/);
});
