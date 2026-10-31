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
globalThis.Option = class {};

await import("../custom_components/reminders/frontend/reminders-panel-polish.js");
const Panel = customElements.get("reminders-management-panel");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

function makePanel() {
  const panel = new Panel();
  panel._items = [];
  panel._history = [];
  panel._view = "upcoming";
  panel._scope = "mine";
  panel._selectedUser = "";
  panel._query = "";
  panel._listTotal = 0;
  panel._historyTotal = 0;
  panel._loading = false;
  panel._renderList = () => {};
  panel._renderFilters = () => {};
  panel._clearError = () => {};
  panel._showError = (error) => { panel._testError = error; };
  return panel;
}

test("newer list request wins when responses arrive out of order", async () => {
  const panel = makePanel();
  const requests = new Map();
  panel._call = (_action, data) => {
    const request = deferred();
    requests.set(data.query, request);
    return request.promise;
  };

  panel._query = "old";
  const older = panel._load();
  panel._query = "new";
  const newer = panel._load();

  assert.equal(requests.size, 2);
  requests.get("new").resolve({ reminders: [{ id: "new" }], total: 1 });
  await newer;
  requests.get("old").resolve({ reminders: [{ id: "old" }], total: 1 });
  await older;

  assert.deepEqual(panel._items, [{ id: "new" }]);
  assert.equal(panel._listTotal, 1);
  assert.equal(panel._loading, false);
});

test("history refresh is not dropped behind an older request", async () => {
  const panel = makePanel();
  panel._view = "history";
  const requests = new Map();
  panel._call = (_action, data) => {
    const request = deferred();
    requests.set(data.query, request);
    return request.promise;
  };

  panel._query = "old";
  const older = panel._load();
  panel._query = "new";
  const newer = panel._load();

  assert.equal(requests.size, 2);
  requests.get("new").resolve({ history: [{ id: "new" }], total: 1 });
  await newer;
  requests.get("old").resolve({ history: [{ id: "old" }], total: 1 });
  await older;

  assert.deepEqual(panel._history, [{ id: "new" }]);
  assert.equal(panel._historyTotal, 1);
});

test("refresh supersedes an in-flight load-more page", async () => {
  const panel = makePanel();
  panel._items = [{ id: "first" }];
  panel._listTotal = 3;
  const requests = [];
  panel._call = (_action, data) => {
    const request = deferred();
    requests.push({ data, ...request });
    return request.promise;
  };

  const append = panel._load({ appendList: true });
  panel._query = "fresh";
  const refresh = panel._load();

  assert.equal(requests[0].data.offset, 1);
  assert.equal(requests[1].data.offset, 0);
  requests[1].resolve({ reminders: [{ id: "fresh" }], total: 1 });
  await refresh;
  requests[0].resolve({ reminders: [{ id: "stale-page" }], total: 3 });
  await append;

  assert.deepEqual(panel._items, [{ id: "fresh" }]);
  assert.equal(panel._listTotal, 1);
});


test("routine list refresh does not replace the focused search field", () => {
  const panel = makePanel();
  panel._hass = { user: { is_admin: false } };
  panel._view = "upcoming";
  panel._scope = "mine";
  panel._selectedUser = "";
  panel._query = "remind";
  panel._filtersKey = "upcoming|mine||false";
  const search = { value: "reminder in progress" };
  let replaced = false;
  const host = {
    childElementCount: 1,
    querySelector: () => search,
    replaceChildren: () => { replaced = true; },
  };
  panel.shadowRoot = {
    activeElement: search,
    querySelector: (selector) => selector === "#filters" ? host : null,
  };
  panel._renderFilters();
  assert.equal(replaced, false);
  assert.equal(search.value, "reminder in progress");
});


test("automatic refresh preserves previously loaded list depth", async () => {
  const panel = makePanel();
  panel._view = "upcoming";
  panel._items = Array.from({ length: 150 }, (_, i) => ({ id: String(i) }));
  panel._loadedFilterKey = JSON.stringify(["upcoming", "mine", "", undefined, "all"]);
  let requestedLimit = 0;
  panel._call = async (_action, data) => {
    requestedLimit = data.limit;
    return { reminders: panel._items, total: 175 };
  };
  await panel._load();
  assert.equal(requestedLimit, 150);
  assert.equal(panel._items.length, 150);
});

test("changing search resets loaded list depth", async () => {
  const panel = makePanel();
  panel._view = "upcoming";
  panel._items = Array.from({ length: 150 }, (_, i) => ({ id: String(i) }));
  panel._loadedFilterKey = JSON.stringify(["upcoming", "mine", "", undefined, "all"]);
  panel._query = "new search";
  let requestedLimit = 0;
  panel._call = async (_action, data) => {
    requestedLimit = data.limit;
    return { reminders: [{ id: "matching" }], total: 1 };
  };
  await panel._load();
  assert.equal(requestedLimit, 100);
  assert.deepEqual(panel._items, [{ id: "matching" }]);
});


test("history outcome filter is forwarded and changes invalidate page depth", async () => {
  const panel = makePanel();
  panel._view = "history";
  panel._historyOutcomeFilter = "failed";
  panel._history = Array.from({ length: 85 }, (_, i) => ({ id: String(i) }));
  panel._loadedFilterKey = JSON.stringify(["history", "mine", "", undefined, "all"]);
  let seen;
  panel._call = async (_action, data) => {
    seen = data;
    return { history: [{ id: "failed-item" }], total: 1 };
  };
  await panel._load();
  assert.deepEqual(seen.statuses, ["failed"]);
  assert.equal(seen.limit, 50);
  assert.equal(panel._history.length, 1);
});


test("history outcome selector survives the generic view filter cleanup", () => {
  class Element {
    constructor(tag = "div") {
      this.tag = tag;
      this.children = [];
      this.classList = { add() {} };
      this.dataset = {};
      this.value = "";
    }
    append(...items) { this.children.push(...items); }
    prepend(...items) { this.children.unshift(...items); }
    replaceChildren(...items) { this.children = items; }
    remove() { this.removed = true; }
    add(option) { this.children.push(option); }
    querySelector(selector) {
      if (selector === ".view-filter-wrap" || selector === ".history-outcome-wrap") {
        return this.children.find((item) => item.className?.includes(selector.slice(1)) && !item.removed) || null;
      }
      return null;
    }
  }
  const panel = makePanel();
  delete panel._renderFilters;
  panel._hass = { user: { is_admin: false } };
  panel._view = "history";
  const tabs = new Element();
  const toolbar = new Element();
  const filters = new Element();
  filters.append = () => {};
  const root = {
    querySelector(selector) {
      return { "#filters": filters, ".tabs": tabs, ".toolbar": toolbar }[selector] || null;
    },
    append() {},
  };
  panel.shadowRoot = root;
  globalThis.document = { createElement: (tag) => new Element(tag) };
  globalThis.Option = class { constructor(label, value) { this.label = label; this.value = value; } };
  // Base rendering requires a minimal filters host; the wrapper must keep the
  // outcome selector after both filter-removal paths run.
  panel._renderFilters();
  assert.ok(toolbar.querySelector(".history-outcome-wrap"));
});

test("Upcoming distinguishes actionable deliveries, context waits and overdue reminders", () => {
  const originalDocument = globalThis.document;
  try {
    const createElement = (tag) => ({
      tag,
      className: "",
      textContent: "",
      children: [],
      append(...nodes) { this.children.push(...nodes); },
      replaceChildren(...nodes) { this.children = nodes; },
    });
    globalThis.document = { createElement };
    const host = createElement("section");
    const panel = makePanel();
    delete panel._renderList;
    panel._view = "upcoming";
    panel._hass = { config: { time_zone: "UTC" } };
    panel._listTotal = 4;
    panel._items = [
      { id: "waiting", status: "waiting_for_context", activation_type: "time", due: "2026-01-01T12:00:00Z" },
      { id: "overdue", status: "pending", activation_type: "time", due: "2026-01-02T12:00:00Z" },
      { id: "dismiss", status: "awaiting_acknowledgement", activation_type: "time", due: "2026-01-03T12:00:00Z" },
      { id: "done", status: "delivered", activation_type: "time", due: "2026-01-04T12:00:00Z" },
    ];
    panel._reminderCard = (reminder) => ({ tag: "card", id: reminder.id });
    panel.shadowRoot = {
      querySelector(selector) { return selector === "#list" ? host : null; },
    };
    panel._renderList();
    assert.deepEqual(
      host.children.filter((node) => node.tag === "h2").map((node) => node.textContent),
      ["Awaiting dismissal", "Action available", "Waiting for conditions", "Overdue"],
    );
    assert.deepEqual(
      host.children.filter((node) => node.tag === "card").map((node) => node.id),
      ["dismiss", "done", "waiting", "overdue"],
    );
  } finally {
    globalThis.document = originalDocument;
  }
});

test("notification deep links fetch only the authorized reminder detail", async () => {
  const panel = makePanel();
  panel._linkedReminderId = "target-reminder";
  const actions = [];
  panel._call = async (name, payload) => {
    actions.push([name, payload]);
    if (name === "list") return { reminders: [], total: 0 };
    if (name === "get") return { reminder: { id: "target-reminder", title: "Authorized" } };
    throw new Error("Unexpected WebSocket call");
  };
  await panel._load();
  assert.equal(panel._linkedReminder.id, "target-reminder");
  assert.equal(actions[1][0], "get");
  assert.deepEqual(actions[1][1], { reminder_id: "target-reminder" });
});

test("an unauthorized or deleted deep-linked reminder is never shown", async () => {
  const panel = makePanel();
  panel._linkedReminderId = "unavailable";
  panel._linkedReminder = { id: "stale", title: "Stale private data" };
  panel._call = async (name) => {
    if (name === "list") return { reminders: [], total: 0 };
    throw new Error("Not authorized");
  };
  await panel._load();
  assert.equal(panel._linkedReminder, null);
});

test("notification-linked reminder appears even when omitted from the current page", () => {
  const previousDocument = globalThis.document;
  try {
    const element = (tag) => ({
      tag, children: [], className: "", textContent: "",
      append(...nodes) { this.children.push(...nodes); },
      replaceChildren(...nodes) { this.children = nodes; },
    });
    globalThis.document = { createElement: element };
    const list = element("section");
    const panel = makePanel();
    delete panel._renderList;
    panel._loading = false;
    panel._view = "upcoming";
    panel._linkedReminder = { id: "linked", title: "Open me" };
    panel._linkedReminderId = "linked";
    panel._items = [];
    panel._reminderCard = (item) => ({ tag: "reminder-card", id: item.id });
    panel.shadowRoot = { querySelector: (selector) => selector === "#list" ? list : null };
    panel._renderList();
    assert.equal(list.children[1].id, "linked");
    const heading = list.children[0];
    assert.equal(heading.children[0].textContent, "Opened from notification");
    heading.children[1].onclick();
    assert.equal(panel._linkedReminder, null);
    assert.equal(panel._linkedReminderId, null);
  } finally {
    globalThis.document = previousDocument;
  }
});

test("stale failed notification lookup never clears a newer successful lookup", async () => {
  const panel = makePanel();
  panel._linkedReminderId = "same";
  const requests = [];
  panel._call = (name) => {
    if (name === "list") return Promise.resolve({ reminders: [], total: 0 });
    const pending = deferred();
    requests.push(pending);
    return pending.promise;
  };
  const oldRequest = panel._load();
  await Promise.resolve(); await Promise.resolve();
  const newRequest = panel._load();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(requests.length, 2);
  requests[1].resolve({ reminder: { id: "same", title: "Latest" } });
  await newRequest;
  requests[0].reject(new Error("Old unauthorized result"));
  await oldRequest;
  assert.equal(panel._linkedReminder.title, "Latest");
});

test("disconnected panel discards late list responses", async () => {
  const panel = makePanel();
  panel._items = [{ id: "before-disconnect" }];
  const pending = deferred();
  panel._call = () => pending.promise;
  const load = panel._load();
  panel.disconnectedCallback();
  pending.resolve({ reminders: [{ id: "stale-after-disconnect" }], total: 1 });
  await load;
  assert.deepEqual(panel._items, [{ id: "before-disconnect" }]);
});
