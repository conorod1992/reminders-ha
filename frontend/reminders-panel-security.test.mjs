import assert from "node:assert/strict";
import test from "node:test";

class FakeHTMLElement {
  attachShadow() {
    this.shadowRoot = {};
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
globalThis.Option = class {};

await import("../custom_components/reminders/frontend/reminders-panel.js");
const Panel = customElements.get("reminders-management-panel");

test("snooze title is assigned as text instead of HTML", () => {
  const title = '<img src=x onerror="globalThis.pwned=true">';
  const hint = { textContent: "" };
  const inputs = [{ value: "" }, { value: "" }];
  const controls = new Map([
    [".hint", hint],
    [".cancel", {}],
    [".tomorrow", {}],
    [".custom", {}],
  ]);
  const dialog = {
    markup: "",
    set innerHTML(value) { this.markup = value; },
    get innerHTML() { return this.markup; },
    querySelector: (selector) => controls.get(selector),
    querySelectorAll: (selector) => selector === "input" ? inputs : [],
    showModal: () => {},
  };
  const panel = new Panel();
  panel._hass = { config: { time_zone: "UTC" } };
  panel.shadowRoot = { querySelector: () => dialog };

  panel._openSnooze({
    activation_type: "time",
    recurring: false,
    title,
  });

  assert.equal(hint.textContent, title);
  assert.equal(dialog.markup.includes(title), false);
  assert.equal(globalThis.pwned, undefined);
});
