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
globalThis.document = {
  createElement: () => ({
    classList: { add() {}, remove() {}, toggle() {} },
    querySelector: () => ({}),
    append() {},
  }),
};

await import("../custom_components/reminders/frontend/reminders-panel.js");
const Panel = customElements.get("reminders-management-panel");
let currentForm;
Panel.prototype._openReminderForm = function () {
  this.shadowRoot = { querySelector: () => currentForm };
};
Panel.prototype._updateBehaviorSummary = function () {};
Panel.prototype._saveReminder = async function (_dialog, form) {
  return this._call("update", {
    reminder_id: "scheduled-1",
    title: "Renamed only",
    deliver_when: form.elements.deliver_when_enabled.checked ? { type: "state" } : null,
    complete_when: form.elements.complete_when_enabled.checked ? { type: "state" } : null,
  });
};
Panel.prototype._call = async function (action, data) {
  this.calls.push({ action, data });
  return { reminder: { id: data.reminder_id || "scheduled-1" } };
};
await import("../custom_components/reminders/frontend/reminders-panel-native.js");

test("editing a scheduled reminder keeps existing native delivery and completion rules", async () => {
  const panel = new Panel();
  panel._hass = { user: { is_admin: true } };
  panel.calls = [];
  currentForm = {
    elements: {
      activation_type: { value: "time" },
      deliver_when_enabled: { checked: false },
      complete_when_enabled: { checked: false },
    },
    querySelector: () => null,
    append() {},
  };
  const source = {
    id: "scheduled-1",
    delivery_conditions: [{ condition: "state", entity_id: "binary_sensor.home", state: "on" }],
    completion_triggers: [{ trigger: "state", entity_id: "sensor.task", to: "done" }],
  };
  panel._openReminderForm(source);
  assert.equal(currentForm.elements.deliver_when_enabled.checked, true);
  assert.equal(currentForm.elements.complete_when_enabled.checked, true);
  await panel._saveReminder(null, currentForm, source);
  const saved = panel.calls.find((call) => call.action === "set_native_rules");
  assert.deepEqual(saved?.data?.delivery_conditions, source.delivery_conditions);
  assert.deepEqual(saved?.data?.completion_triggers, source.completion_triggers);
});

test("native rules remain disabled when no such rules were supplied", () => {
  const panel = new Panel();
  panel._hass = { user: { is_admin: true } };
  currentForm = {
    elements: {
      activation_type: { value: "time" },
      deliver_when_enabled: { checked: false },
      complete_when_enabled: { checked: false },
    },
    querySelector: () => null,
    append() {},
  };
  panel._openReminderForm({ id: "plain" });
  assert.equal(currentForm.elements.deliver_when_enabled.checked, false);
  assert.equal(currentForm.elements.complete_when_enabled.checked, false);
});
