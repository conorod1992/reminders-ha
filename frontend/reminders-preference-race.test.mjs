import assert from 'node:assert/strict';
import test from 'node:test';
globalThis.HTMLElement = class { attachShadow() { this.shadowRoot = {}; } toggleAttribute() {} };
const registry = new Map();
globalThis.customElements = { get: n => registry.get(n), define: (n, c) => registry.set(n,c) };
globalThis.document = { createElement: () => ({append(){}, classList:{add(){},remove(){},toggle(){}}, querySelector:()=>({})}) };
await import('../custom_components/reminders/frontend/reminders-panel.js');
const Panel = registry.get('reminders-management-panel');
const deferred = () => { let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}; };

// Exercise actual preferences dialog control flow with delayed WebSocket replies.
test("preference replies remain bound to the selected owner", async () => {
const panel = new Panel();
panel._hass = {user:{id:'admin',is_admin:true}};
const select = {};
const controls = new Map();
const dialog = {set innerHTML(v){},querySelector(s){if(!controls.has(s))controls.set(s,{append(){}});return controls.get(s);},querySelectorAll(){return[];},open:true,close(){this.open=false;}};
panel.shadowRoot = {querySelector:()=>dialog};
panel._userSelect = () => select;
panel._showDialog = () => {};
panel._populatePreferences = (_dialog,prefs) => { panel.displayed = prefs.user; };
panel._savePreferences = async (_dialog,target) => {panel.saved={target, valuesFrom:panel.displayed};return true;};
const replies = new Map();
panel._call = (_cmd,data) => {const p=deferred();replies.set(data.user_id,p);return p.promise;};
const open = panel._openPreferences(false);
replies.get('admin').resolve({preferences:{user:'admin'}});await open;
select.value='alice';select.onchange();
select.value='bob';select.onchange();
replies.get('bob').resolve({preferences:{user:'bob'}});await Promise.resolve();
replies.get('alice').resolve({preferences:{user:'alice'}});await Promise.resolve();
await dialog.querySelector('.save').onclick();
assert.deepEqual(panel.saved,{target:'bob',valuesFrom:'bob'});
});

