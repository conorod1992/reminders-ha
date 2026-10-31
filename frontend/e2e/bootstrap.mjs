// Used only with a fresh disposable CI instance. Never point this at a household HA.
import { writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";
const origin = process.env.HA_URL;
if (origin !== "http://localhost:8123" || !process.env.HA_STORAGE_STATE) {
  throw new Error("Bootstrap requires the disposable localhost instance and an auth-state output path");
}
const clientId = origin + "/";
async function request(path, data, token) {
  const response = await fetch(origin + path, {
    method: data === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}
let ready = false;
for (let attempt = 0; attempt < 180; attempt++) {
  try { await request("/api/onboarding"); ready = true; break; } catch {}
  await new Promise(resolve => setTimeout(resolve, 1000));
}
if (!ready) throw new Error("Home Assistant did not start");
const onboarding = await request("/api/onboarding");
if (onboarding.some(step => step.done)) throw new Error("Refusing to bootstrap an existing installation");
const { auth_code: code } = await request("/api/onboarding/users", {
  client_id: clientId, name: "Review administrator", username: "review", password: randomUUID(), language: "en",
});
const response = await fetch(origin + "/auth/token", {
  method: "POST", body: new URLSearchParams({ grant_type: "authorization_code", code, client_id: clientId }),
});
if (!response.ok) throw new Error("Token exchange failed");
const tokens = await response.json();
for (const step of ["core_config", "analytics", "integration"]) {
  await request(`/api/onboarding/${step}`, step === "integration" ? { client_id: clientId, redirect_uri: clientId } : {}, tokens.access_token);
}
const flow = await request("/api/config/config_entries/flow", { handler: "reminders", show_advanced_options: false }, tokens.access_token);
const result = await request(`/api/config/config_entries/flow/${flow.flow_id}`, {}, tokens.access_token);
if (result.type !== "create_entry") throw new Error("Reminders setup failed");
let configured = false;
for (let attempt = 0; attempt < 60; attempt++) {
  try {
    await request("/api/services/reminders/set_user_preferences", {channels:["persistent_notification"], notify_targets:[],voice_targets:[],mobile_app_services:[]}, tokens.access_token);
    configured = true; break;
  } catch { await new Promise(resolve => setTimeout(resolve, 1000)); }
}
if (!configured) throw new Error("Reminders did not become ready");
await writeFile(process.env.HA_STORAGE_STATE, JSON.stringify({ cookies: [], origins: [{ origin, localStorage: [{ name: "hassTokens", value: JSON.stringify({ ...tokens, hassUrl: origin, clientId, expires: Date.now() + tokens.expires_in * 1000 }) }] }] }));
