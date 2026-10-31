// Used only with a fresh disposable CI instance. Never point this at a household HA.
import { writeFile, appendFile } from "node:fs/promises";
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


async function command(message) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(origin.replace("http", "ws") + "/api/websocket");
    const timer = setTimeout(() => { socket.close(); reject(new Error("WebSocket bootstrap timeout")); }, 30000);
    socket.onerror = () => { clearTimeout(timer); reject(new Error("WebSocket bootstrap failed")); };
    socket.onmessage = event => {
      const data = JSON.parse(event.data);
      if (data.type === "auth_required") socket.send(JSON.stringify({type:"auth",access_token:tokens.access_token}));
      else if (data.type === "auth_ok") socket.send(JSON.stringify({id:1,...message}));
      else if (data.type === "result") { clearTimeout(timer); socket.close(); data.success ? resolve(data.result) : reject(new Error(data.error?.message)); }
    };
  });
}
if (process.env.HA_NONADMIN_STORAGE_STATE) {
  const ordinary = await command({type:"config/auth/create", name:"Review ordinary user",group_ids:["system-users"]});
  const password = randomUUID();
  await command({type:"config/auth_provider/homeassistant/create", user_id:ordinary.user.id,username:"ordinary",password});
  const login = await request("/auth/login_flow", {client_id:clientId,handler:["homeassistant",null],redirect_uri:clientId});
  const loggedIn = await request(`/auth/login_flow/${login.flow_id}`, {client_id:clientId,username:"ordinary",password});
  const tokenResponse = await fetch(origin + "/auth/token", {method:"POST",body:new URLSearchParams({grant_type:"authorization_code",code:loggedIn.result,client_id:clientId})});
  if (!tokenResponse.ok) throw new Error("Ordinary user token exchange failed");
  const ordinaryTokens = await tokenResponse.json();
  await request("/api/services/reminders/set_user_preferences", {channels:["persistent_notification"],notify_targets:[],voice_targets:[],mobile_app_services:[]}, ordinaryTokens.access_token);
  await writeFile(process.env.HA_NONADMIN_STORAGE_STATE, JSON.stringify({cookies:[],origins:[{origin,localStorage:[{name:"hassTokens",value:JSON.stringify({...ordinaryTokens,hassUrl:origin,clientId,expires:Date.now()+ordinaryTokens.expires_in*1000})}]}]}));
  const sentinel = await command({type:"reminders/create",title:"Private administrator sentinel",due:new Date(Date.now()+86400000).toISOString()});
  if (process.env.GITHUB_ENV) await appendFile(process.env.GITHUB_ENV, `HA_OTHER_USER_REMINDER_ID=${sentinel.reminder.id}\n`);
}
