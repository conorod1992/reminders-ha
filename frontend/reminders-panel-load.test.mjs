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
