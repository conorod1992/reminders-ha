import {
  WEEKDAYS, deliverySummary, localDateTime, recurrenceSummary, zonedInputParts,
} from "./reminders-utils.js";

const TEXT = {
  title: "Reminders", add: "Add reminder", preferences: "Preferences",
  upcoming: "Upcoming", recurring: "Recurring", failed: "Failed",
  emptyUpcoming: "No upcoming reminders", emptyRecurring: "No recurring reminders",
  emptyFailed: "No failed reminders", edit: "Edit", snooze: "Snooze", delete: "Delete",
};

class RemindersManagementPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._reminders = [];
    this._users = [];
    this._view = "upcoming";
    this._scope = "mine";
    this._selectedUser = "";
    this._loading = true;
    this._started = false;
  }

  set hass(value) {
    this._hass = value;
    if (this.isConnected && !this._started) this._start();
  }
  get hass() { return this._hass; }
  set narrow(value) { this.toggleAttribute("narrow", Boolean(value)); }
  set panel(value) { this._panel = value; }
  set route(value) { this._route = value; }

  connectedCallback() {
    this._renderShell();
    if (this._hass && !this._started) this._start();
  }

  disconnectedCallback() {
    if (this._unsubscribe) this._unsubscribe();
    this._unsubscribe = undefined;
    this._started = false;
  }

  async _start() {
    this._started = true;
    if (this._hass.user?.is_admin) {
      try { this._users = (await this._call("users")).users; } catch (err) { this._showError(err); }
    }
    this._renderFilters();
    await this._load();
    try {
      this._unsubscribe = await this._hass.connection.subscribeMessage(
        () => this._load(), { type: "reminders/subscribe" },
      );
    } catch (err) { this._showError(err); }
  }

  _call(command, data = {}) {
    return this._hass.callWS({ type: `reminders/${command}`, ...data });
  }

  async _load() {
    this._loading = true;
    this._renderList();
    try {
      const data = { scope: this._scope, view: this._view };
      if (this._scope === "user") data.user_id = this._selectedUser;
      this._reminders = (await this._call("list", data)).reminders;
      this._clearError();
    } catch (err) {
      this._showError(err);
    } finally {
      this._loading = false;
      this._renderList();
    }
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;min-height:100%;background:var(--primary-background-color);color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif)}
        *{box-sizing:border-box}.page{max-width:1050px;margin:auto;padding:24px}.top{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.top h1{margin:0 auto 0 0;font-size:28px}
        button,.button{border:0;border-radius:6px;padding:10px 16px;background:var(--primary-color);color:var(--text-primary-color,#fff);font:inherit;cursor:pointer}button.secondary{background:transparent;color:var(--primary-color);border:1px solid var(--divider-color)}button.danger{color:var(--error-color);background:transparent;border:1px solid var(--error-color)}button:disabled{opacity:.55;cursor:wait}
        .filters{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:24px 0 16px;border-bottom:1px solid var(--divider-color)}.tab{border:0;border-radius:0;background:transparent;color:var(--secondary-text-color);padding:12px}.tab.active{color:var(--primary-color);border-bottom:3px solid var(--primary-color)}.scope{margin-left:auto;display:flex;gap:8px;align-items:center;padding-bottom:6px}
        select,input,textarea{font:inherit;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:6px;padding:10px;width:100%}select[multiple]{min-height:92px}label{display:grid;gap:6px;color:var(--secondary-text-color)}textarea{min-height:78px;resize:vertical}.fieldrow{display:grid;grid-template-columns:1fr 1fr;gap:12px}.checkrow{display:flex;flex-wrap:wrap;gap:14px}.checkrow label{display:flex;align-items:center;gap:6px;color:var(--primary-text-color)}input[type=checkbox],input[type=radio]{width:auto}
        .list{display:grid;gap:12px}.card{display:grid;grid-template-columns:150px 1fr auto;gap:18px;align-items:center;padding:18px;background:var(--card-background-color);border-radius:10px;box-shadow:var(--ha-card-box-shadow,0 2px 4px rgba(0,0,0,.12))}.when{font-weight:500}.name{font-size:18px;font-weight:500}.meta,.message{margin-top:5px;color:var(--secondary-text-color);line-height:1.4}.actions{display:flex;gap:6px;flex-wrap:wrap}.actions button{padding:7px 10px;background:transparent;color:var(--primary-color)}.actions .danger{color:var(--error-color)}
        .empty,.loading{padding:48px 16px;text-align:center;color:var(--secondary-text-color)}.empty p{font-size:18px}.error{display:none;padding:12px 16px;margin:16px 0;border-left:4px solid var(--error-color);background:var(--card-background-color)}.error.show{display:block}
        dialog{width:min(620px,calc(100vw - 24px));max-height:calc(100vh - 40px);overflow:auto;border:0;border-radius:12px;padding:0;background:var(--card-background-color);color:var(--primary-text-color);box-shadow:0 8px 28px rgba(0,0,0,.35)}dialog::backdrop{background:rgba(0,0,0,.45)}.dialog{padding:22px}.dialog h2{margin:0 0 18px}.form{display:grid;gap:16px}.dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:20px}.hint{font-size:13px;color:var(--secondary-text-color)}.hidden{display:none!important}.spinner{display:inline-block;width:22px;height:22px;border:3px solid var(--divider-color);border-top-color:var(--primary-color);border-radius:50%;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}
        @media(max-width:700px){.page{padding:16px}.card{grid-template-columns:1fr;gap:8px}.actions{border-top:1px solid var(--divider-color);padding-top:8px}.scope{width:100%;margin-left:0}.scope select{flex:1}.fieldrow{grid-template-columns:1fr}}
      </style>
      <main class="page">
        <div class="top"><h1>${TEXT.title}</h1><button id="prefs" class="secondary">${TEXT.preferences}</button><button id="add">＋ ${TEXT.add}</button></div>
        <div id="error" class="error" role="alert"></div><div id="filters" class="filters"></div><section id="list" class="list" aria-live="polite"></section>
      </main><dialog id="dialog"></dialog>`;
    this.shadowRoot.querySelector("#add").addEventListener("click", () => this._openReminderForm());
    this.shadowRoot.querySelector("#prefs").addEventListener("click", () => this._openPreferences());
    this._renderFilters(); this._renderList();
  }

  _renderFilters() {
    const host = this.shadowRoot?.querySelector("#filters"); if (!host) return;
    host.replaceChildren();
    for (const [view, label] of [["upcoming",TEXT.upcoming],["recurring",TEXT.recurring],["failed",TEXT.failed]]) {
      const button = document.createElement("button"); button.className = `tab${view === this._view ? " active" : ""}`; button.textContent = label;
      button.setAttribute("aria-pressed", String(view === this._view));
      button.addEventListener("click", () => { this._view = view; this._renderFilters(); this._load(); }); host.append(button);
    }
    if (this._hass?.user?.is_admin) {
      const wrap = document.createElement("div"); wrap.className = "scope";
      const label = document.createElement("label"); label.textContent = "View"; label.setAttribute("for", "scope");
      const select = document.createElement("select"); select.id = "scope";
      [["mine","My reminders"],["all","All users"],["user","Specific user"]].forEach(([value,text]) => { const option=new Option(text,value,false,value===this._scope); select.add(option); });
      select.addEventListener("change", () => { this._scope=select.value; if(this._scope==="user"&&!this._selectedUser)this._selectedUser=this._users[0]?.id||""; this._renderFilters(); this._load(); }); wrap.append(label,select);
      if (this._scope === "user") { const users=this._userSelect(this._selectedUser); users.setAttribute("aria-label","Specific user"); users.addEventListener("change",()=>{this._selectedUser=users.value;this._load();}); wrap.append(users); }
      host.append(wrap);
    }
  }

  _renderList() {
    const host = this.shadowRoot?.querySelector("#list"); if (!host) return; host.replaceChildren();
    if (this._loading) { const div=document.createElement("div");div.className="loading";div.innerHTML='<span class="spinner" aria-label="Loading"></span>';host.append(div);return; }
    if (!this._reminders.length) { const div=document.createElement("div");div.className="empty";const p=document.createElement("p");p.textContent={upcoming:TEXT.emptyUpcoming,recurring:TEXT.emptyRecurring,failed:TEXT.emptyFailed}[this._view];div.append(p);if(this._view==="upcoming"){const b=document.createElement("button");b.textContent=TEXT.add;b.onclick=()=>this._openReminderForm();div.append(b);}host.append(div);return; }
    for (const reminder of this._reminders) host.append(this._reminderCard(reminder));
  }

  _reminderCard(reminder) {
    const card=document.createElement("article");card.className="card";
    const when=document.createElement("div");when.className="when";when.textContent=this._formatDate(reminder.due);
    const body=document.createElement("div");const name=document.createElement("div");name.className="name";name.textContent=reminder.title;body.append(name);
    if(reminder.message){const message=document.createElement("div");message.className="message";message.textContent=reminder.message.length>120?`${reminder.message.slice(0,117)}…`:reminder.message;body.append(message);}
    const meta=document.createElement("div");meta.className="meta";const values=[recurrenceSummary(reminder,this._hass.locale?.language),deliverySummary(reminder)];if(reminder.owner_name)values.push(reminder.owner_name);if(reminder.status==="failed"||reminder.last_occurrence_status==="failed")values.push("Failed to deliver");meta.textContent=values.join(" · ");body.append(meta);
    const actions=document.createElement("div");actions.className="actions";actions.append(this._action(TEXT.edit,()=>this._openReminderForm(reminder)),this._action(TEXT.snooze,()=>this._openSnooze(reminder)),this._action(TEXT.delete,()=>this._confirmDelete(reminder),"danger"));
    card.append(when,body,actions);return card;
  }

  _action(label, handler, className="") { const button=document.createElement("button");button.textContent=label;button.className=className;button.addEventListener("click",handler);return button; }
  _formatDate(value) { try{return new Intl.DateTimeFormat(this._hass.locale?.language,{dateStyle:"medium",timeStyle:"short",timeZone:this._hass.config.time_zone}).format(new Date(value));}catch{return value;} }
  _userSelect(selected) { const select=document.createElement("select");for(const user of this._users){const option=new Option(user.name,user.id,false,user.id===selected);select.add(option);}return select; }
  _entitySelect(domain, selected=[]) { const select=document.createElement("select");select.multiple=true;for(const [id,state] of Object.entries(this._hass.states||{}).filter(([id])=>id.startsWith(`${domain}.`))){const option=new Option(state.attributes.friendly_name||id,id,false,selected.includes(id));select.add(option);}return select; }
  _selected(select) { return [...select.selectedOptions].map((option)=>option.value); }

  _openReminderForm(reminder=null) {
    const recurring=Boolean(reminder?.recurring);const rule=reminder?.recurrence;const zone=rule?.timezone||this._hass.config.time_zone;
    const parts=rule?{date:rule.anchor_local.slice(0,10),time:rule.anchor_local.slice(11,16)}:zonedInputParts(reminder?.due,zone);
    const dialog=this.shadowRoot.querySelector("#dialog");dialog.innerHTML=`<div class="dialog"><h2>${reminder?"Edit reminder":"Add reminder"}</h2><form class="form" id="reminder-form">
      ${reminder?"":'<label>Reminder type<select name="kind"><option value="one">One-time</option><option value="recurring">Recurring</option></select></label>'}
      <label>Title<input name="title" required maxlength="255"></label><label>Message (optional)<textarea name="message" maxlength="4000"></textarea></label>
      <div class="fieldrow"><label class="date-label">${recurring?"First reminder date":"Due date"}<input name="date" type="date" required></label><label class="time-label">${recurring?"First reminder time":"Due time"}<input name="time" type="time" required></label></div>
      <div id="recurrence" class="hidden"><div class="fieldrow"><label>Repeat<select name="frequency"><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option></select></label><label>Every<input name="interval" type="number" min="1" value="1" required></label></div><div id="weekly" class="hidden"><span>On these days</span><div class="checkrow"></div></div><label id="monthly" class="hidden">Day of month<input name="day_of_month" type="number" min="1" max="31"><span class="hint">Months without this day are skipped.</span></label><details><summary>Advanced</summary><label>Timezone<input name="timezone"></label></details><p class="hint next"></p></div>
      <div id="owner"></div><label>Delivery<select name="delivery_mode"><option value="default">Use my defaults</option><option value="custom">Custom</option></select></label><div id="custom-delivery" class="hidden"><div class="checkrow channels"></div><label class="notify-label hidden">Phone notification targets</label><label class="voice-label hidden">Voice targets</label></div>
      <div class="dialog-actions"><button type="button" class="secondary cancel">Cancel</button><button type="submit">Save</button></div></form></div>`;
    const form=dialog.querySelector("form");form.elements.title.value=reminder?.title||"";form.elements.message.value=reminder?.message||"";form.elements.date.value=parts.date;form.elements.time.value=parts.time;
    const recurrence=dialog.querySelector("#recurrence");const kind=form.elements.kind;const setRecurring=(active)=>{recurrence.classList.toggle("hidden",!active);dialog.querySelector(".date-label").childNodes[0].textContent=active?"First reminder date":"Due date";dialog.querySelector(".time-label").childNodes[0].textContent=active?"First reminder time":"Due time";};if(kind){kind.onchange=()=>{setRecurring(kind.value==="recurring");if(kind.value==="recurring")this._defaultRecurrenceDate(form,false);this._syncRecurrence(form);};}setRecurring(recurring);
    for(const day of WEEKDAYS){const label=document.createElement("label");const input=document.createElement("input");input.type="checkbox";input.name="weekday";input.value=day;input.checked=rule?.weekdays?.includes(day)||false;label.append(input,day[0].toUpperCase()+day.slice(1));dialog.querySelector("#weekly .checkrow").append(label);}
    form.elements.frequency.value=rule?.frequency||"daily";form.elements.interval.value=rule?.interval||1;form.elements.day_of_month.value=rule?.day_of_month||Number(parts.date.slice(8,10));form.elements.timezone.value=zone;form.elements.frequency.onchange=()=>this._syncRecurrence(form);form.elements.date.onchange=()=>this._defaultRecurrenceDate(form,Boolean(reminder));this._syncRecurrence(form);
    if(this._hass.user?.is_admin){const ownerWrap=dialog.querySelector("#owner");const label=document.createElement("label");label.textContent="Recipient";const owner=this._userSelect(reminder?.user_id||this._hass.user.id);owner.name="user_id";label.append(owner);ownerWrap.append(label);}
    const policy=reminder?.delivery_policy;form.elements.delivery_mode.value=policy?"custom":"default";const channels=dialog.querySelector(".channels");for(const [value,labelText] of [["phone","Phone"],["voice","Voice"],["persistent_notification","Persistent notification"]]){const label=document.createElement("label");const input=document.createElement("input");input.type="checkbox";input.name="channel";input.value=value;input.checked=policy?.channels?.includes(value)||false;input.onchange=()=>this._syncDelivery(dialog);label.append(input,labelText);channels.append(label);}
    const notify=this._entitySelect("notify",policy?.notify_targets);notify.name="notify_targets";dialog.querySelector(".notify-label").append(notify);const voice=this._entitySelect("assist_satellite",policy?.voice_targets);voice.name="voice_targets";dialog.querySelector(".voice-label").append(voice);form.elements.delivery_mode.onchange=()=>this._syncDelivery(dialog);this._syncDelivery(dialog);
    dialog.querySelector(".cancel").onclick=()=>dialog.close();form.onsubmit=(event)=>{event.preventDefault();this._saveReminder(dialog,form,reminder);};dialog.showModal();form.elements.title.focus();
  }

  _defaultRecurrenceDate(form, editing) { if(editing)return;const date=new Date(`${form.elements.date.value}T12:00:00`);const day=WEEKDAYS[(date.getDay()+6)%7];for(const input of form.querySelectorAll('[name=weekday]'))input.checked=input.value===day;form.elements.day_of_month.value=Number(form.elements.date.value.slice(8,10)); }
  _syncRecurrence(form) { const frequency=form.elements.frequency.value;form.querySelector("#weekly").classList.toggle("hidden",frequency!=="weekly");form.querySelector("#monthly").classList.toggle("hidden",frequency!=="monthly"); }
  _syncDelivery(dialog) { const form=dialog.querySelector("form");const custom=form.elements.delivery_mode.value==="custom";dialog.querySelector("#custom-delivery").classList.toggle("hidden",!custom);const selected=[...form.querySelectorAll('[name=channel]:checked')].map((el)=>el.value);dialog.querySelector(".notify-label").classList.toggle("hidden",!selected.includes("phone"));dialog.querySelector(".voice-label").classList.toggle("hidden",!selected.includes("voice")); }

  async _saveReminder(dialog,form,reminder) {
    const save=form.querySelector('[type=submit]');save.disabled=true;const kind=reminder?(reminder.recurring?"recurring":"one"):form.elements.kind.value;
    const data={title:form.elements.title.value,message:form.elements.message.value||null};if(form.elements.user_id)data.user_id=form.elements.user_id.value;
    if(form.elements.delivery_mode.value==="custom"){data.delivery_mode="custom";data.channels=[...form.querySelectorAll('[name=channel]:checked')].map((el)=>el.value);data.notify_targets=this._selected(form.elements.notify_targets);data.voice_targets=this._selected(form.elements.voice_targets);}else data.delivery_mode="default";
    if(kind==="recurring"){data.first_reminder=localDateTime(form.elements.date.value,form.elements.time.value);data.frequency=form.elements.frequency.value;data.interval=Number(form.elements.interval.value);data.timezone=form.elements.timezone.value;if(data.frequency==="weekly")data.weekdays=[...form.querySelectorAll('[name=weekday]:checked')].map((el)=>el.value);if(data.frequency==="monthly")data.day_of_month=Number(form.elements.day_of_month.value);}else data.due=localDateTime(form.elements.date.value,form.elements.time.value);
    try{if(reminder){data.reminder_id=reminder.id;await this._call("update",data);}else await this._call(kind==="recurring"?"create_recurring":"create",data);dialog.close();await this._load();}catch(err){this._showError(err);save.disabled=false;}
  }

  _openSnooze(reminder) { const dialog=this.shadowRoot.querySelector("#dialog");dialog.innerHTML=`<div class="dialog"><h2>Snooze “<span class="title"></span>”</h2>${reminder.recurring?'<p class="hint">Only the current occurrence moves; the recurring schedule remains unchanged.</p>':""}<div class="form presets"><button data-seconds="600">10 minutes</button><button data-seconds="1800">30 minutes</button><button data-seconds="3600">1 hour</button><button data-tomorrow="true">Tomorrow</button><label>Custom date and time<div class="fieldrow"><input type="date"><input type="time"></div></label></div><div class="dialog-actions"><button class="secondary cancel">Cancel</button><button class="custom">Snooze</button></div></div>`;dialog.querySelector(".title").textContent=reminder.title;const parts=zonedInputParts(new Date(Date.now()+3600000).toISOString(),this._hass.config.time_zone);const inputs=dialog.querySelectorAll("input");inputs[0].value=parts.date;inputs[1].value=parts.time;dialog.querySelector(".cancel").onclick=()=>dialog.close();for(const button of dialog.querySelectorAll("[data-seconds]"))button.onclick=()=>this._doSnooze(dialog,reminder,{duration_seconds:Number(button.dataset.seconds)});dialog.querySelector("[data-tomorrow]").onclick=()=>{const tomorrow=zonedInputParts(new Date(Date.now()+86400000).toISOString(),this._hass.config.time_zone);this._doSnooze(dialog,reminder,{due:localDateTime(tomorrow.date,"09:00")});};dialog.querySelector(".custom").onclick=()=>this._doSnooze(dialog,reminder,{due:localDateTime(inputs[0].value,inputs[1].value)});dialog.showModal(); }
  async _doSnooze(dialog,reminder,data){try{await this._call("snooze",{reminder_id:reminder.id,...data});dialog.close();await this._load();}catch(err){this._showError(err);}}
  _confirmDelete(reminder){const dialog=this.shadowRoot.querySelector("#dialog");dialog.innerHTML='<div class="dialog"><h2></h2><p></p><div class="dialog-actions"><button class="secondary cancel">Cancel</button><button class="danger confirm">Delete</button></div></div>';dialog.querySelector("h2").textContent=`Delete ${reminder.recurring?"recurring reminder ":""}“${reminder.title}”?`;dialog.querySelector("p").textContent=reminder.recurring?"This will remove the entire recurring series and all future occurrences.":"This reminder will be permanently removed.";dialog.querySelector(".cancel").onclick=()=>dialog.close();dialog.querySelector(".confirm").onclick=async()=>{try{await this._call("delete",{reminder_id:reminder.id});dialog.close();await this._load();}catch(err){this._showError(err);}};dialog.showModal();}

  async _openPreferences(){const dialog=this.shadowRoot.querySelector("#dialog");let target=this._hass.user.id;dialog.innerHTML='<div class="dialog"><h2>Reminder preferences</h2><div class="form"><div id="preference-user"></div><div class="checkrow channels"></div><label class="notify-label">Phone notification targets</label><label class="voice-label">Voice targets</label></div><div class="dialog-actions"><button class="secondary cancel">Cancel</button><button class="save">Save</button></div></div>';if(this._hass.user.is_admin){const label=document.createElement("label");label.textContent="Preferences for";const select=this._userSelect(target);label.append(select);dialog.querySelector("#preference-user").append(label);select.onchange=()=>load(select.value);}
    const load=async(userId)=>{target=userId;try{const result=await this._call("get_preferences",{user_id:target});const policy=result.preferences.default_delivery_policy;const channels=dialog.querySelector(".channels");channels.replaceChildren();for(const [value,text] of [["phone","Phone"],["voice","Voice"],["persistent_notification","Persistent notification"]]){const label=document.createElement("label");const input=document.createElement("input");input.type="checkbox";input.value=value;input.checked=policy.channels.includes(value);label.append(input,text);channels.append(label);}const oldNotify=dialog.querySelector('[name=notify]');if(oldNotify)oldNotify.remove();const notify=this._entitySelect("notify",policy.notify_targets);notify.name="notify";dialog.querySelector(".notify-label").append(notify);const oldVoice=dialog.querySelector('[name=voice]');if(oldVoice)oldVoice.remove();const voice=this._entitySelect("assist_satellite",policy.voice_targets);voice.name="voice";dialog.querySelector(".voice-label").append(voice);}catch(err){this._showError(err);}};dialog.querySelector(".cancel").onclick=()=>dialog.close();dialog.querySelector(".save").onclick=async()=>{const channels=[...dialog.querySelectorAll('.channels input:checked')].map((el)=>el.value);try{await this._call("set_preferences",{user_id:target,channels,notify_targets:this._selected(dialog.querySelector('[name=notify]')),voice_targets:this._selected(dialog.querySelector('[name=voice]'))});dialog.close();}catch(err){this._showError(err);}};dialog.showModal();await load(target);}

  _showError(error){const host=this.shadowRoot?.querySelector("#error");if(!host)return;host.textContent=error?.message||String(error);host.classList.add("show");host.scrollIntoView({behavior:"smooth",block:"nearest"});}
  _clearError(){const host=this.shadowRoot?.querySelector("#error");if(host){host.textContent="";host.classList.remove("show");}}
}

if (!customElements.get("reminders-management-panel")) customElements.define("reminders-management-panel", RemindersManagementPanel);
