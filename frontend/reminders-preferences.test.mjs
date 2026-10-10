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
globalThis.Option = class {
  constructor(text, value, _defaultSelected, selected) {
    this.text = text;
    this.value = value;
    this.selected = selected;
  }
};
globalThis.document = {
  createElement: (tag) => {
    if (tag !== "select") throw new Error("Unexpected element");
    return {
      multiple: false,
      options: [],
      add(option) { this.options.push(option); },
    };
  },
};
await import("../custom_components/reminders/frontend/reminders-panel.js");
const Panel = customElements.get("reminders-management-panel");

test("missing selected notify entities remain available for editing", () => {
  const panel = new Panel();
  panel._hass = { states: { "notify.kitchen": { attributes: { friendly_name: "Kitchen" } } } };
  const select = panel._entitySelect("notify", ["notify.missing", "notify.kitchen"]);
  assert.equal(select.multiple, true);
  assert.deepEqual(
    select.options.filter((option) => option.selected).map((option) => option.value).sort(),
    ["notify.kitchen", "notify.missing"],
  );
  assert.match(select.options.find((option) => option.value === "notify.missing").text, /unavailable/);
});

test("saving ordinary preferences omits hidden advanced quiet-hours overrides", () => {
  const panel = new Panel();
  panel._selected = () => [];
  const entries = {
    "[name=require_acknowledgement]": { checked: true },
    "[name=quiet_enabled]": { checked: true },
    "[name=quiet_start]": { value: "23:00" },
    "[name=quiet_end]": { value: "07:00" },
    "[name=retention_days]": { value: "90" },
    "[name=retention_count]": { value: "250" },
  };
  const dialog = {
    querySelectorAll: () => [{ value: "phone" }],
    querySelector: (selector) => entries[selector] || {},
  };
  const payload = panel._preferencesPayload(dialog, "u1");
  assert.equal(payload.quiet_hours_channels, undefined);
  assert.equal(payload.quiet_hours_fallback_channels, undefined);
  assert.equal(payload.quiet_hours_enabled, true);
});
