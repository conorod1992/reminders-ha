import assert from "node:assert/strict";
import test from "node:test";

class FakeHTMLElement {
  attachShadow() { this.shadowRoot = {}; return this.shadowRoot; }
  toggleAttribute() {}
}
const elements = new Map();
globalThis.HTMLElement = FakeHTMLElement;
globalThis.customElements = {
  define: (name, value) => elements.set(name, value),
  get: (name) => elements.get(name),
};
globalThis.Option = class {
  constructor(label, value) { this.label = label; this.value = value; }
};

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.className = "";
    this.dataset = {};
    this.attrs = {};
    this.value = "";
    this.classList = { add() {} };
  }
  get childElementCount() { return this.children.length; }
  setAttribute(name, value) { this.attrs[name] = value; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  prepend(...children) { this.children.unshift(...children); }
  querySelector(selector) {
    if (selector === 'input[type="search"]') return this.children.find((c) => c.type === "search") || null;
    if (selector === ".view-filter-wrap") return this.children.find((c) => c.className === "view-filter-wrap") || null;
    return null;
  }
  querySelectorAll(selector) {
    return selector === '[role="tab"]' ? this.children.filter((c) => c.attrs.role === "tab") : [];
  }
  focus() { this.focused = true; }
  click() { this.onclick?.(); }
}
globalThis.document = { createElement: (tag) => new Element(tag) };

await import("../custom_components/reminders/frontend/reminders-panel-polish.js");
const Panel = elements.get("reminders-management-panel");

test("view tabs expose selection state and support arrow/Home/End keyboard navigation", () => {
  const panel = new Panel();
  panel._hass = { user: { is_admin: false } };
  panel._view = "upcoming";
  panel._scope = "mine";
  panel._query = "";
  const filters = new Element("div");
  panel.shadowRoot = {
    querySelector(selector) {
      if (selector === "#filters") return filters;
      if (selector === ".tabs") return filters.children.find((el) => el.className === "tabs");
      if (selector === ".toolbar") return filters.children.find((el) => el.className === "toolbar");
      return null;
    },
    append() {},
  };
  panel._load = async () => {};
  panel._renderFilters();
  const tabs = panel.shadowRoot.querySelector(".tabs");
  const buttons = tabs.querySelectorAll('[role="tab"]');
  assert.equal(tabs.attrs.role, "tablist");
  assert.equal(buttons.length, 4);
  assert.equal(buttons[1].attrs["aria-selected"], "true");
  assert.equal(buttons[1].tabIndex, 0);
  let prevented = false;
  buttons[1].onkeydown({ key: "ArrowRight", preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(panel._view, "all");
  assert.equal(buttons[2].focused, true);
  buttons[2].onkeydown({ key: "Home", preventDefault() {} });
  assert.equal(panel._view, "attention");
  buttons[0].onkeydown({ key: "End", preventDefault() {} });
  assert.equal(panel._view, "history");
});
