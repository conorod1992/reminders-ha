import assert from "node:assert/strict";
import test from "node:test";

class FakeElement {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.className = "";
    this.value = "";
    this._text = "";
  }
  set textContent(value) { this._text = value; }
  get textContent() { return this._text; }
  set innerHTML(value) {
    this.markup = value;
    if (value.includes("overflow-menu")) this._menu = new FakeElement("div");
  }
  append(...nodes) { this.children.push(...nodes); }
  querySelector(selector) {
    if (selector === ".overflow-menu") return this._menu;
    return null;
  }
}

class FakeHTMLElement {
  attachShadow() { this.shadowRoot = {}; return this.shadowRoot; }
  toggleAttribute() {}
}
const registry = new Map();
globalThis.HTMLElement = FakeHTMLElement;
globalThis.customElements = {
  define(name, constructor) { registry.set(name, constructor); },
  get(name) { return registry.get(name); },
};
globalThis.document = { createElement: (tag) => new FakeElement(tag) };
await import("../custom_components/reminders/frontend/reminders-panel.js");
const Panel = customElements.get("reminders-management-panel");

function makePanel() {
  const panel = new Panel();
  panel._hass = { user: { id: "owner" }, config: { time_zone: "UTC" }, locale: { language: "en" } };
  panel._preferences = { default_delivery_policy: { channels: ["persistent_notification"] } };
  panel._formatDate = (value) => value;
  panel._complete = async (_r, id) => { panel.doneId = id; };
  panel._acknowledge = async (_r, id) => { panel.dismissId = id; };
  return panel;
}
function reminder(history) {
  return {
    id: "series",
    title: "Take medication",
    user_id: "owner",
    activation_type: "time",
    status: "pending",
    due: "2099-10-31T12:00:00Z",
    allow_manual_completion: true,
    current_occurrence_id: "next",
    recurring: true,
    recurrence: { frequency: "daily", interval: 1, anchor_local: "2026-10-01T12:00:00" },
    occurrence_history: [
      ...history,
      { id: "next", status: "scheduled", scheduled_due: "2099-10-31T12:00:00Z" },
    ],
  };
}
const past = [
  { id: "previous-1", status: "awaiting_acknowledgement", scheduled_due: "2026-10-01T12:00:00Z" },
  { id: "previous-2", status: "delivered", scheduled_due: "2026-10-02T12:00:00Z" },
];

test("multiple unresolved recurring occurrences expose dated, targeted buttons", async () => {
  const panel = makePanel();
  const card = panel._reminderCard(reminder(past));
  const body = card.children[1];
  const primaryActions = card.children[2].children.filter((node) => node.tag === "button");
  assert.deepEqual(primaryActions.map((node) => node.textContent), ["Done now", "Snooze"]);
  const details = body.children.find((node) => node.className === "outstanding-occurrences");
  assert.ok(details);
  assert.equal(details.children[0].textContent, "2 earlier occurrences need action");
  const firstRow = details.children[1];
  assert.equal(firstRow.children[0].textContent, "2026-10-02T12:00:00Z");
  await firstRow.children[1].children[0].onclick();
  assert.equal(panel.doneId, "previous-2");
  const secondRow = details.children[2];
  await secondRow.children[1].children[1].onclick();
  assert.equal(panel.dismissId, "previous-1");
});

test("a single unresolved occurrence keeps direct Done and Dismiss actions", async () => {
  const panel = makePanel();
  const card = panel._reminderCard(reminder(past.slice(0, 1)));
  const actions = card.children[2].children.filter((node) => node.tag === "button");
  assert.deepEqual(actions.map((node) => node.textContent), ["Done now", "Done", "Snooze", "Dismiss"]);
  await actions.find((node) => node.textContent === "Dismiss").onclick();
  assert.equal(panel.dismissId, "previous-1");
});

test("many outstanding occurrences are bounded and link to existing History view", async () => {
  const panel = makePanel();
  const history = Array.from({ length: 12 }, (_, n) => ({
    id: `occ-${n}`, status: "awaiting_acknowledgement",
    scheduled_due: `2026-10-${String(n + 1).padStart(2, "0")}T10:00:00Z`,
  }));
  const card = panel._reminderCard(reminder(history));
  const details = card.children[1].children.find((node) => node.className === "outstanding-occurrences");
  assert.ok(details);
  assert.equal(details.children.filter((node) => node.className === "outstanding-row").length, 5);
  const historyButton = details.children.at(-1);
  panel._load = async () => {};
  historyButton.onclick();
  assert.equal(panel._view, "history");
});
