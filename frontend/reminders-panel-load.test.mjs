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
