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
  define: (name, ctor) => registry.set(name, ctor),
  get: (name) => registry.get(name),
};
globalThis.Option = class {};
await import("../custom_components/reminders/frontend/reminders-panel-polish.js");
const Panel = registry.get("reminders-management-panel");

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function panelFixture() {
  const panel = new Panel();
  panel.isConnected = true;
  panel._hass = { user: { is_admin: false }, connection: { subscribeMessage: async () => () => {} } };
  panel._renderList = () => {};
  panel._showError = () => {};
  panel._clearError = () => {};
  panel._load = async () => true;
  panel._call = async () => ({ preferences: { configured: true } });
  return panel;
}

test("disconnect during preferences fetch cannot subscribe or open a dialog", async () => {
  const panel = panelFixture();
  const pending = deferred();
  let subscribed = 0;
  let opened = 0;
  panel._call = () => pending.promise;
  panel._hass.connection.subscribeMessage = async () => { subscribed++; return () => {}; };
  panel._openPreferences = () => { opened++; };
  const startup = panel._start();
  panel.isConnected = false;
  panel.disconnectedCallback();
  pending.resolve({ preferences: { configured: false } });
  await startup;
  assert.equal(subscribed, 0);
  assert.equal(opened, 0);
  assert.equal(panel._started, false);
  assert.equal(panel._startupInFlight, false);
});

test("late subscription is immediately released after disconnect", async () => {
  const panel = panelFixture();
  const pending = deferred();
  let unsubscribes = 0;
  const subscribed = deferred();
  panel._hass.connection.subscribeMessage = () => {
    subscribed.resolve();
    return pending.promise;
  };
  const startup = panel._start();
  // Wait until the subscription request has genuinely started.
  await subscribed.promise;
  panel.isConnected = false;
  panel.disconnectedCallback();
  pending.resolve(() => { unsubscribes++; });
  await startup;
  assert.equal(unsubscribes, 1);
  assert.equal(panel._unsubscribe, undefined);
  assert.equal(panel._started, false);
});

test("stale startup cannot replace the new connection on reconnect", async () => {
  const panel = panelFixture();
  const old = deferred();
  let calls = 0;
  panel._call = () => (++calls === 1 ? old.promise : Promise.resolve({ preferences: { configured: true } }));
  const first = panel._start();
  panel.isConnected = false;
  panel.disconnectedCallback();
  panel.isConnected = true;
  const second = panel._start();
  await second;
  old.resolve({ preferences: { configured: true } });
  await first;
  assert.equal(panel._started, true);
  assert.equal(panel._startupInFlight, false);
  assert.equal(panel._preferences.configured, true);
});
