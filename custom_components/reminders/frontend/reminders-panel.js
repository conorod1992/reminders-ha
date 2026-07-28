import {
  WEEKDAYS,
  acknowledgementSummary,
  awaitingOccurrences,
  deliverySummary,
  localDateTime,
  quickTimeParts,
  recurrenceSummary,
  zonedInputParts,
} from "./reminders-utils.js";

const CHANNELS = [
  ["persistent_notification", "Home Assistant notification"],
  ["phone", "Phone notification"],
  ["voice", "Voice announcement"],
];

class RemindersManagementPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._items = [];
    this._history = [];
    this._users = [];
    this._view = "upcoming";
    this._scope = "mine";
    this._selectedUser = "";
    this._query = "";
    this._loading = true;
    this._started = false;
    this._preferences = null;
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
    try {
      if (this._hass.user?.is_admin) this._users = (await this._call("users")).users;
      this._preferences = (await this._call("get_preferences")).preferences;
      await this._load();
      this._unsubscribe = await this._hass.connection.subscribeMessage(
        () => this._load(),
        { type: "reminders/subscribe" },
      );
      if (!this._preferences.configured) this._openPreferences(true);
    } catch (error) {
      this._showError(error);
    }
  }

  _call(command, data = {}) {
    return this._hass.callWS({ type: `reminders/${command}`, ...data });
  }

  async _load() {
    this._loading = true;
    this._renderList();
    try {
      const data = { scope: this._scope, query: this._query || undefined };
      if (this._scope === "user") data.user_id = this._selectedUser;
      if (this._view === "history") {
        data.limit = 100;
        const result = await this._call("history", data);
        this._history = result.history;
      } else {
        data.view = this._view;
        const result = await this._call("list", data);
        this._items = result.reminders;
      }
      this._clearError();
    } catch (error) {
      this._showError(error);
    } finally {
      this._loading = false;
      this._renderFilters();
      this._renderList();
    }
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;min-height:100%;background:var(--primary-background-color);color:var(--primary-text-color);font-family:var(--paper-font-body1_-_font-family,Roboto,sans-serif)}
        *{box-sizing:border-box}.page{max-width:1080px;margin:auto;padding:24px}.top,.toolbar,.actions,.quick,.dialog-actions,.checkrow{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.top h1{margin:0 auto 0 0;font-size:28px}
        button{border:0;border-radius:8px;padding:10px 15px;background:var(--primary-color);color:var(--text-primary-color,#fff);font:inherit;cursor:pointer}button.secondary,.actions button,.quick button{background:transparent;color:var(--primary-color);border:1px solid var(--divider-color)}button.danger{color:var(--error-color);background:transparent;border:1px solid var(--error-color)}button:disabled{opacity:.55;cursor:wait}
        .filters{display:grid;gap:12px;margin:22px 0 16px}.tabs{display:flex;border-bottom:1px solid var(--divider-color);overflow:auto}.tab{border:0;border-radius:0;background:transparent;color:var(--secondary-text-color);padding:12px}.tab.active{color:var(--primary-color);border-bottom:3px solid var(--primary-color)}.toolbar input{flex:1;min-width:220px}.scope{display:flex;gap:8px;align-items:center}
        select,input,textarea{font:inherit;color:var(--primary-text-color);background:var(--card-background-color);border:1px solid var(--divider-color);border-radius:7px;padding:10px;width:100%}select[multiple]{min-height:100px}label{display:grid;gap:6px;color:var(--secondary-text-color)}textarea{min-height:76px;resize:vertical}.fieldrow{display:grid;grid-template-columns:1fr 1fr;gap:12px}.checkrow label{display:flex;align-items:center;gap:6px;color:var(--primary-text-color)}input[type=checkbox],input[type=radio]{width:auto}
        .list{display:grid;gap:12px}.card{display:grid;grid-template-columns:165px 1fr auto;gap:18px;align-items:center;padding:18px;background:var(--card-background-color);border-radius:12px;box-shadow:var(--ha-card-box-shadow,0 2px 4px rgba(0,0,0,.12))}.when{font-weight:500}.name{font-size:18px;font-weight:500}.meta,.message,.hint{margin-top:5px;color:var(--secondary-text-color);line-height:1.4}.actions{justify-content:flex-end}.actions button{padding:7px 10px}.status{display:inline-block;border-radius:999px;padding:3px 8px;background:var(--secondary-background-color);font-size:12px;text-transform:capitalize}.status.awaiting_acknowledgement{color:var(--warning-color)}.status.failed{color:var(--error-color)}
        .empty,.loading{padding:48px 16px;text-align:center;color:var(--secondary-text-color)}.error{display:none;padding:12px 16px;margin:16px 0;border-left:4px solid var(--error-color);background:var(--card-background-color)}.error.show{display:block}.success{color:var(--success-color);padding:8px 0}
        dialog{width:min(680px,calc(100vw - 24px));max-height:calc(100vh - 32px);overflow:auto;border:0;border-radius:12px;padding:0;background:var(--card-background-color);color:var(--primary-text-color);box-shadow:0 8px 28px rgba(0,0,0,.35)}dialog::backdrop{background:rgba(0,0,0,.45)}.dialog{padding:22px}.dialog h2{margin:0 0 8px}.form{display:grid;gap:16px}.dialog-actions{justify-content:flex-end;margin-top:20px}.hidden{display:none!important}details{border-top:1px solid var(--divider-color);padding-top:12px}summary{cursor:pointer;color:var(--primary-color);margin-bottom:14px}.advanced{display:grid;gap:16px}.quick button.selected{background:var(--primary-color);color:#fff}.preview{padding:10px;border-radius:8px;background:var(--secondary-background-color)}
        @media(max-width:720px){.page{padding:16px}.card{grid-template-columns:1fr;gap:8px}.actions{justify-content:flex-start;border-top:1px solid var(--divider-color);padding-top:8px}.fieldrow{grid-template-columns:1fr}.toolbar,.scope{width:100%}.scope select{flex:1}}
      </style>
      <main class="page">
        <div class="top"><h1>Reminders</h1><button id="prefs" class="secondary">Preferences</button><button id="add">+ Add reminder</button></div>
        <div id="error" class="error" role="alert"></div>
        <div id="filters" class="filters"></div>
        <section id="list" class="list" aria-live="polite"></section>
      </main><dialog id="dialog"></dialog>`;
    this.shadowRoot.querySelector("#add").onclick = () => this._openReminderForm();
    this.shadowRoot.querySelector("#prefs").onclick = () => this._openPreferences(false);
    this._renderFilters();
    this._renderList();
  }

  _renderFilters() {
    const host = this.shadowRoot?.querySelector("#filters");
    if (!host) return;
    host.replaceChildren();
    const tabs = document.createElement("div");
    tabs.className = "tabs";
    for (const [view, label] of [["upcoming", "Upcoming"], ["recurring", "Recurring"], ["history", "History"], ["failed", "Failed"]]) {
      const button = document.createElement("button");
      button.className = `tab${view === this._view ? " active" : ""}`;
      button.textContent = label;
      button.onclick = () => { this._view = view; this._load(); };
      tabs.append(button);
    }
    const toolbar = document.createElement("div");
    toolbar.className = "toolbar";
    const search = document.createElement("input");
    search.type = "search";
    search.placeholder = this._view === "history" ? "Search history" : "Search title or message";
    search.value = this._query;
    let timer;
    search.oninput = () => {
      clearTimeout(timer);
      timer = setTimeout(() => { this._query = search.value.trim(); this._load(); }, 250);
    };
    toolbar.append(search);
    if (this._hass?.user?.is_admin) toolbar.append(this._scopeControls());
    host.append(tabs, toolbar);
  }

  _scopeControls() {
    const wrap = document.createElement("div");
    wrap.className = "scope";
    const select = document.createElement("select");
    for (const [value, text] of [["mine", "My reminders"], ["all", "All users"], ["user", "Specific user"]]) {
      select.add(new Option(text, value, false, value === this._scope));
    }
    select.onchange = () => {
      this._scope = select.value;
      if (this._scope === "user" && !this._selectedUser) this._selectedUser = this._users[0]?.id || "";
      this._load();
    };
    wrap.append(select);
    if (this._scope === "user") {
      const users = this._userSelect(this._selectedUser);
      users.onchange = () => { this._selectedUser = users.value; this._load(); };
      wrap.append(users);
    }
    return wrap;
  }

  _renderList() {
    const host = this.shadowRoot?.querySelector("#list");
    if (!host) return;
    host.replaceChildren();
    if (this._loading) { host.innerHTML = '<div class="loading">Loading...</div>'; return; }
    const values = this._view === "history" ? this._history : this._items;
    if (!values.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.innerHTML = `<p>${this._query ? "No matching reminders" : this._view === "history" ? "No reminder history yet" : "No reminders here"}</p>`;
      host.append(empty);
      return;
    }
    for (const item of values) host.append(this._view === "history" ? this._historyCard(item) : this._reminderCard(item));
  }

  _reminderCard(reminder) {
    const card = document.createElement("article");
    card.className = "card";
    const when = document.createElement("div");
    when.className = "when";
    when.textContent = this._formatDate(reminder.due);
    const body = document.createElement("div");
    const title = document.createElement("div");
    title.className = "name";
    title.textContent = reminder.title;
    body.append(title);
    if (reminder.message) {
      const message = document.createElement("div");
      message.className = "message";
      message.textContent = reminder.message;
      body.append(message);
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    const values = [recurrenceSummary(reminder, this._hass.locale?.language), deliverySummary(reminder), acknowledgementSummary(reminder)];
    if (reminder.owner_name) values.push(reminder.owner_name);
    meta.textContent = values.join(" · ");
    body.append(meta);
    const actions = document.createElement("div");
    actions.className = "actions";
    const awaiting = awaitingOccurrences(reminder);
    if (awaiting.length) actions.append(this._action("Done", () => this._acknowledge(reminder, awaiting[awaiting.length - 1].id)));
    actions.append(
      this._action("Edit", () => this._openReminderForm(reminder)),
      this._action("Duplicate", () => this._openReminderForm(null, reminder)),
      this._action("Snooze", () => this._openSnooze(reminder)),
      this._action("Delete", () => this._confirmDelete(reminder), "danger"),
    );
    card.append(when, body, actions);
    return card;
  }

  _historyCard(row) {
    const occurrence = row.occurrence;
    const card = document.createElement("article");
    card.className = "card";
    const when = document.createElement("div");
    when.className = "when";
    when.textContent = this._formatDate(occurrence.scheduled_due);
    const body = document.createElement("div");
    const title = document.createElement("div");
    title.className = "name";
    title.textContent = row.title;
    const status = document.createElement("span");
    status.className = `status ${occurrence.status}`;
    status.textContent = occurrence.status.replaceAll("_", " ");
    const details = document.createElement("div");
    details.className = "meta";
    const channelText = occurrence.succeeded_channels.length ? `Delivered by ${occurrence.succeeded_channels.join(", ")}` : "No channel succeeded";
    const extras = [channelText];
    if (occurrence.failed_channels.length) extras.push(`Failed: ${occurrence.failed_channels.join(", ")}`);
    if (occurrence.suppressed_channels.length) extras.push(`Quiet hours suppressed: ${occurrence.suppressed_channels.join(", ")}`);
    if (occurrence.snoozed) extras.push("Snoozed");
    if (row.owner_name) extras.push(row.owner_name);
    details.textContent = extras.join(" · ");
    body.append(title, status, details);
    const actions = document.createElement("div");
    actions.className = "actions";
    if (occurrence.status === "awaiting_acknowledgement") actions.append(this._action("Done", () => this._acknowledge({ id: row.reminder_id }, occurrence.id)));
    card.append(when, body, actions);
    return card;
  }

  _action(label, handler, className = "") {
    const button = document.createElement("button");
    button.textContent = label;
    button.className = className;
    button.onclick = handler;
    return button;
  }

  _openReminderForm(reminder = null, duplicate = null) {
    const source = reminder || duplicate;
    const recurring = Boolean(source?.recurring);
    const rule = source?.recurrence;
    const zone = rule?.timezone || this._hass.config.time_zone;
    const dueParts = rule
      ? { date: rule.anchor_local.slice(0, 10), time: rule.anchor_local.slice(11, 16) }
      : duplicate
        ? { date: "", time: "" }
        : zonedInputParts(source?.due, zone);
    const dialog = this.shadowRoot.querySelector("#dialog");
    dialog.innerHTML = `<div class="dialog"><h2>${reminder ? "Edit reminder" : duplicate ? "Duplicate reminder" : "Add reminder"}</h2>
      ${duplicate && !recurring ? '<p class="hint">Choose a new time before saving this copy.</p>' : ""}
      <form id="reminder-form" class="form">
        <label>Title<input name="title" required maxlength="255"></label>
        <div class="quick" ${reminder || recurring ? "hidden" : ""}><button type="button" data-quick="10m">10 minutes</button><button type="button" data-quick="30m">30 minutes</button><button type="button" data-quick="1h">1 hour</button><button type="button" data-quick="later">Later today</button><button type="button" data-quick="tomorrow">Tomorrow morning</button></div>
        <div class="fieldrow"><label>Date<input name="date" type="date" required></label><label>Time<input name="time" type="time" required></label></div>
        <details ${recurring ? "open" : ""}><summary>Advanced options</summary><div class="advanced">
          <label>Message (optional)<textarea name="message" maxlength="4000"></textarea></label>
          ${reminder ? "" : '<label class="repeat-toggle"><span><input name="repeat" type="checkbox"> Repeat this reminder</span></label>'}
          <div id="recurrence" class="hidden">
            <div class="fieldrow"><label>Repeat<select name="frequency"><option value="daily">Daily</option><option value="weekly">Weekly</option><option value="monthly">Monthly</option><option value="yearly">Yearly</option></select></label><label>Every<input name="interval" type="number" min="1" value="1"></label></div>
            <div id="weekly" class="hidden"><span>Days</span><div class="checkrow weekdays"></div><div class="quick"><button type="button" data-days="weekday">Weekdays</button><button type="button" data-days="weekend">Weekends</button></div></div>
            <div id="monthly" class="hidden"><label>Monthly pattern<select name="monthly_mode"><option value="day_of_month">Day of month</option><option value="nth_weekday">Nth weekday</option><option value="last_weekday">Last weekday</option><option value="last_day">Last calendar day</option></select></label><div class="fieldrow monthly-pattern"><label class="day-field">Day<input name="day_of_month" type="number" min="1" max="31"></label><label class="week-field hidden">Which<input name="monthly_week" type="number" min="1" max="5"></label><label class="weekday-field hidden">Weekday<select name="monthly_weekday">${WEEKDAYS.map((day) => `<option value="${day}">${this._capitalize(day)}</option>`).join("")}</select></label></div></div>
            <div class="fieldrow"><label>End date (optional)<input name="end_date" type="date"></label><label>Stop after (optional)<input name="occurrence_count" type="number" min="1" placeholder="occurrences"></label></div>
            <label>Timezone<input name="timezone"></label><button type="button" class="secondary preview-button">Preview next dates</button><div class="preview hidden"></div>
          </div>
          <div id="owner"></div>
          <label>Delivery<select name="delivery_mode"><option value="default">Use my defaults</option><option value="custom">Choose for this reminder</option></select></label>
          <div id="custom-delivery" class="hidden"><div class="checkrow channels"></div><label class="notify-label hidden">Phones</label><label class="voice-label hidden">Voice devices</label></div>
          <label>Completion tracking<select name="acknowledgement_policy"><option value="default">Use my default</option><option value="required">Require me to mark it done</option><option value="not_required">Do not require completion</option></select></label>
          <label>Quiet hours<select name="quiet_hours_policy"><option value="respect">Respect my quiet hours</option><option value="ignore">Ignore quiet hours (urgent)</option></select></label>
        </div></details>
        <div class="dialog-actions"><button type="button" class="secondary cancel">Cancel</button><button type="submit">Save</button></div>
      </form></div>`;
    const form = dialog.querySelector("form");
    form.elements.title.value = source?.title || "";
    form.elements.message.value = source?.message || "";
    form.elements.date.value = dueParts.date;
    form.elements.time.value = dueParts.time;
    if (form.elements.repeat) form.elements.repeat.checked = recurring;
    for (const day of WEEKDAYS) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox"; input.name = "weekday"; input.value = day;
      input.checked = rule?.weekdays?.includes(day) || false;
      label.append(input, this._capitalize(day));
      dialog.querySelector(".weekdays").append(label);
    }
    form.elements.frequency.value = rule?.frequency || "daily";
    form.elements.interval.value = rule?.interval || 1;
    form.elements.monthly_mode.value = rule?.monthly_mode || "day_of_month";
    form.elements.day_of_month.value = rule?.day_of_month || Number(dueParts.date.slice(8, 10)) || 1;
    form.elements.monthly_week.value = rule?.monthly_week || 1;
    form.elements.monthly_weekday.value = rule?.monthly_weekday || "monday";
    form.elements.end_date.value = rule?.end_date || "";
    form.elements.occurrence_count.value = rule?.occurrence_count || "";
    form.elements.timezone.value = zone;
    form.elements.acknowledgement_policy.value = source?.acknowledgement_policy || "default";
    form.elements.quiet_hours_policy.value = source?.quiet_hours_policy || "respect";
    if (this._hass.user?.is_admin) {
      const label = document.createElement("label");
      label.textContent = "Recipient";
      const select = this._userSelect(source?.user_id || this._hass.user.id);
      select.name = "user_id";
      label.append(select);
      dialog.querySelector("#owner").append(label);
    }
    const policy = source?.delivery_policy;
    form.elements.delivery_mode.value = policy ? "custom" : "default";
    for (const [value, text] of CHANNELS) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox"; input.name = "channel"; input.value = value;
      input.checked = policy?.channels?.includes(value) || false;
      input.onchange = () => this._syncDelivery(dialog);
      label.append(input, text);
      dialog.querySelector(".channels").append(label);
    }
    const notify = this._entitySelect("notify", policy?.notify_targets || []);
    notify.name = "notify_targets";
    dialog.querySelector(".notify-label").append(notify);
    const voice = this._entitySelect("assist_satellite", policy?.voice_targets || []);
    voice.name = "voice_targets";
    dialog.querySelector(".voice-label").append(voice);
    for (const button of dialog.querySelectorAll("[data-quick]")) button.onclick = () => {
      const parts = quickTimeParts(button.dataset.quick, this._hass.config.time_zone);
      form.elements.date.value = parts.date; form.elements.time.value = parts.time;
      for (const other of dialog.querySelectorAll("[data-quick]")) other.classList.toggle("selected", other === button);
    };
    for (const button of dialog.querySelectorAll("[data-days]")) button.onclick = () => {
      const selected = button.dataset.days === "weekday" ? WEEKDAYS.slice(0, 5) : WEEKDAYS.slice(5);
      for (const input of form.querySelectorAll("[name=weekday]")) input.checked = selected.includes(input.value);
    };
    if (form.elements.repeat) form.elements.repeat.onchange = () => this._syncRecurrence(form, form.elements.repeat.checked);
    form.elements.frequency.onchange = () => this._syncRecurrence(form, true);
    form.elements.monthly_mode.onchange = () => this._syncRecurrence(form, true);
    form.elements.delivery_mode.onchange = () => this._syncDelivery(dialog);
    dialog.querySelector(".preview-button").onclick = () => this._previewRecurrence(form);
    this._syncRecurrence(form, recurring);
    this._syncDelivery(dialog);
    dialog.querySelector(".cancel").onclick = () => dialog.close();
    form.onsubmit = (event) => { event.preventDefault(); this._saveReminder(dialog, form, reminder); };
    dialog.showModal();
    form.elements.title.focus();
  }

  _syncRecurrence(form, active) {
    form.querySelector("#recurrence").classList.toggle("hidden", !active);
    const frequency = form.elements.frequency.value;
    form.querySelector("#weekly").classList.toggle("hidden", !active || frequency !== "weekly");
    form.querySelector("#monthly").classList.toggle("hidden", !active || frequency !== "monthly");
    const mode = form.elements.monthly_mode.value;
    form.querySelector(".day-field").classList.toggle("hidden", mode !== "day_of_month");
    form.querySelector(".week-field").classList.toggle("hidden", mode !== "nth_weekday");
    for (const item of form.querySelectorAll(".weekday-field")) item.classList.toggle("hidden", !["nth_weekday", "last_weekday"].includes(mode));
  }

  _syncDelivery(dialog) {
    const form = dialog.querySelector("form");
    const custom = form.elements.delivery_mode.value === "custom";
    dialog.querySelector("#custom-delivery").classList.toggle("hidden", !custom);
    const selected = [...form.querySelectorAll("[name=channel]:checked")].map((input) => input.value);
    dialog.querySelector(".notify-label").classList.toggle("hidden", !selected.includes("phone"));
    dialog.querySelector(".voice-label").classList.toggle("hidden", !selected.includes("voice"));
  }

  _recurrenceData(form) {
    const data = {
      first_reminder: localDateTime(form.elements.date.value, form.elements.time.value),
      frequency: form.elements.frequency.value,
      interval: Number(form.elements.interval.value),
      timezone: form.elements.timezone.value,
    };
    if (data.frequency === "weekly") data.weekdays = [...form.querySelectorAll("[name=weekday]:checked")].map((input) => input.value);
    if (data.frequency === "monthly") {
      data.monthly_mode = form.elements.monthly_mode.value;
      if (data.monthly_mode === "day_of_month") data.day_of_month = Number(form.elements.day_of_month.value);
      if (["nth_weekday", "last_weekday"].includes(data.monthly_mode)) data.monthly_weekday = form.elements.monthly_weekday.value;
      if (data.monthly_mode === "nth_weekday") data.monthly_week = Number(form.elements.monthly_week.value);
    }
    if (form.elements.end_date.value) data.end_date = form.elements.end_date.value;
    if (form.elements.occurrence_count.value) data.occurrence_count = Number(form.elements.occurrence_count.value);
    return data;
  }

  async _previewRecurrence(form) {
    const host = form.querySelector(".preview");
    host.classList.remove("hidden");
    host.textContent = "Loading preview...";
    try {
      const result = await this._call("preview_recurrence", this._recurrenceData(form));
      host.textContent = result.occurrences.length
        ? result.occurrences.map((value) => this._formatDate(value)).join(" · ")
        : "This rule has no future occurrences.";
    } catch (error) {
      host.textContent = error.message || String(error);
    }
  }

  async _saveReminder(dialog, form, reminder) {
    const save = form.querySelector("[type=submit]");
    save.disabled = true;
    const recurring = reminder?.recurring || form.elements.repeat?.checked;
    const data = {
      title: form.elements.title.value,
      message: form.elements.message.value || null,
      acknowledgement_policy: form.elements.acknowledgement_policy.value,
      quiet_hours_policy: form.elements.quiet_hours_policy.value,
      delivery_mode: form.elements.delivery_mode.value,
    };
    if (form.elements.user_id) data.user_id = form.elements.user_id.value;
    if (data.delivery_mode === "custom") {
      data.channels = [...form.querySelectorAll("[name=channel]:checked")].map((input) => input.value);
      data.notify_targets = this._selected(form.elements.notify_targets);
      data.voice_targets = this._selected(form.elements.voice_targets);
    }
    Object.assign(data, recurring ? this._recurrenceData(form) : { due: localDateTime(form.elements.date.value, form.elements.time.value) });
    if (reminder?.recurring) {
      data.end_date = form.elements.end_date.value || null;
      data.occurrence_count = form.elements.occurrence_count.value ? Number(form.elements.occurrence_count.value) : null;
    }
    try {
      if (reminder) { data.reminder_id = reminder.id; await this._call("update", data); }
      else await this._call(recurring ? "create_recurring" : "create", data);
      dialog.close();
      await this._load();
    } catch (error) {
      this._showError(error);
      save.disabled = false;
    }
  }

  _openSnooze(reminder) {
    const dialog = this.shadowRoot.querySelector("#dialog");
    dialog.innerHTML = `<div class="dialog"><h2>Snooze</h2><p class="hint">${reminder.recurring ? "Only this occurrence moves; the series stays anchored." : reminder.title}</p><div class="quick"><button data-seconds="600">10 minutes</button><button data-seconds="1800">30 minutes</button><button data-seconds="3600">1 hour</button></div><div class="fieldrow"><label>Date<input type="date"></label><label>Time<input type="time"></label></div><div class="dialog-actions"><button class="secondary cancel">Cancel</button><button class="custom">Snooze</button></div></div>`;
    const parts = quickTimeParts("1h", this._hass.config.time_zone);
    const inputs = dialog.querySelectorAll("input"); inputs[0].value = parts.date; inputs[1].value = parts.time;
    dialog.querySelector(".cancel").onclick = () => dialog.close();
    for (const button of dialog.querySelectorAll("[data-seconds]")) button.onclick = () => this._doSnooze(dialog, reminder, { duration_seconds: Number(button.dataset.seconds) });
    dialog.querySelector(".custom").onclick = () => this._doSnooze(dialog, reminder, { due: localDateTime(inputs[0].value, inputs[1].value) });
    dialog.showModal();
  }

  async _doSnooze(dialog, reminder, data) {
    try { await this._call("snooze", { reminder_id: reminder.id, ...data }); dialog.close(); await this._load(); }
    catch (error) { this._showError(error); }
  }

  async _acknowledge(reminder, occurrenceId) {
    try { await this._call("acknowledge", { reminder_id: reminder.id, occurrence_id: occurrenceId }); await this._load(); }
    catch (error) { this._showError(error); }
  }

  _confirmDelete(reminder) {
    const dialog = this.shadowRoot.querySelector("#dialog");
    dialog.innerHTML = `<div class="dialog"><h2>Delete reminder?</h2><p>${reminder.recurring ? "This removes the whole series and its retained history." : "This reminder will be permanently removed."}</p><div class="dialog-actions"><button class="secondary cancel">Cancel</button><button class="danger confirm">Delete</button></div></div>`;
    dialog.querySelector(".cancel").onclick = () => dialog.close();
    dialog.querySelector(".confirm").onclick = async () => {
      try { await this._call("delete", { reminder_id: reminder.id }); dialog.close(); await this._load(); }
      catch (error) { this._showError(error); }
    };
    dialog.showModal();
  }

  async _openPreferences(firstRun) {
    const dialog = this.shadowRoot.querySelector("#dialog");
    let target = this._hass.user.id;
    dialog.innerHTML = `<div class="dialog"><h2>${firstRun ? "Set up reminder delivery" : "Reminder preferences"}</h2><p class="hint">Choose how reminders should reach you. Home Assistant notifications are the simplest reliable option; phone and voice are optional.</p><div class="form"><div id="preference-user"></div><div class="checkrow channels"></div><label class="notify-label">Phones<span class="hint">Devices shown by their friendly Home Assistant names.</span></label><label class="voice-label">Voice devices<span class="hint">Assist satellites that can announce reminders.</span></label><label><span><input type="checkbox" name="require_acknowledgement"> Require me to mark reminders done by default</span></label><details><summary>Quiet hours and history</summary><div class="advanced"><label><span><input type="checkbox" name="quiet_enabled"> Enable quiet hours for voice</span></label><div class="fieldrow"><label>Start<input type="time" name="quiet_start"></label><label>End<input type="time" name="quiet_end"></label></div><div class="fieldrow"><label>Keep history for days<input type="number" name="retention_days" min="1" max="3650"></label><label>Maximum occurrences<input type="number" name="retention_count" min="10" max="5000"></label></div></div></details><div class="quick tests"><button data-test="persistent_notification">Test notification</button><button data-test="phone">Test phone</button><button data-test="voice">Test voice</button><button data-test="all">Test configured delivery</button></div><div class="test-result hint" role="status"></div></div><div class="dialog-actions"><button class="secondary cancel">${firstRun ? "Skip for now" : "Cancel"}</button><button class="save">Save preferences</button></div></div>`;
    if (this._hass.user.is_admin && !firstRun) {
      const label = document.createElement("label"); label.textContent = "Preferences for";
      const select = this._userSelect(target); label.append(select); dialog.querySelector("#preference-user").append(label);
      select.onchange = () => load(select.value);
    }
    const load = async (userId) => {
      target = userId;
      try {
        const prefs = (await this._call("get_preferences", { user_id: target })).preferences;
        this._populatePreferences(dialog, prefs);
      } catch (error) { this._showError(error); }
    };
    dialog.querySelector(".cancel").onclick = async () => {
      if (firstRun) await this._savePreferences(dialog, target, true);
      dialog.close();
    };
    dialog.querySelector(".save").onclick = async () => {
      if (await this._savePreferences(dialog, target, true)) dialog.close();
    };
    for (const button of dialog.querySelectorAll("[data-test]")) button.onclick = () => this._testDelivery(dialog, target, button.dataset.test);
    dialog.showModal();
    await load(target);
  }

  _populatePreferences(dialog, prefs) {
    const channels = dialog.querySelector(".channels"); channels.replaceChildren();
    for (const [value, text] of CHANNELS) {
      const label = document.createElement("label"); const input = document.createElement("input");
      input.type = "checkbox"; input.value = value; input.checked = prefs.default_delivery_policy.channels.includes(value); label.append(input, text); channels.append(label);
    }
    for (const [selector, domain, selected] of [[".notify-label", "notify", prefs.default_delivery_policy.notify_targets], [".voice-label", "assist_satellite", prefs.default_delivery_policy.voice_targets]]) {
      const old = dialog.querySelector(`${selector} select`); if (old) old.remove();
      dialog.querySelector(selector).append(this._entitySelect(domain, selected));
    }
    dialog.querySelector("[name=require_acknowledgement]").checked = prefs.require_acknowledgement;
    dialog.querySelector("[name=quiet_enabled]").checked = prefs.quiet_hours_enabled;
    dialog.querySelector("[name=quiet_start]").value = prefs.quiet_hours_start;
    dialog.querySelector("[name=quiet_end]").value = prefs.quiet_hours_end;
    dialog.querySelector("[name=retention_days]").value = prefs.history_retention_days;
    dialog.querySelector("[name=retention_count]").value = prefs.history_max_occurrences;
  }

  _preferencesPayload(dialog, target) {
    return {
      user_id: target,
      channels: [...dialog.querySelectorAll(".channels input:checked")].map((input) => input.value),
      notify_targets: this._selected(dialog.querySelector(".notify-label select")),
      voice_targets: this._selected(dialog.querySelector(".voice-label select")),
      require_acknowledgement: dialog.querySelector("[name=require_acknowledgement]").checked,
      configured: true,
      quiet_hours_enabled: dialog.querySelector("[name=quiet_enabled]").checked,
      quiet_hours_start: dialog.querySelector("[name=quiet_start]").value,
      quiet_hours_end: dialog.querySelector("[name=quiet_end]").value,
      quiet_hours_channels: ["voice"],
      quiet_hours_fallback_channels: ["persistent_notification"],
      history_retention_days: Number(dialog.querySelector("[name=retention_days]").value),
      history_max_occurrences: Number(dialog.querySelector("[name=retention_count]").value),
    };
  }

  async _savePreferences(dialog, target) {
    try {
      const result = await this._call("set_preferences", this._preferencesPayload(dialog, target));
      if (target === this._hass.user.id) this._preferences = result.preferences;
      return true;
    } catch (error) { this._showError(error); return false; }
  }

  async _testDelivery(dialog, target, channel) {
    const values = this._preferencesPayload(dialog, target);
    const payload = {
      user_id: target,
      channels: channel === "all" ? values.channels : [channel],
      notify_targets: values.notify_targets,
      voice_targets: values.voice_targets,
    };
    const resultHost = dialog.querySelector(".test-result"); resultHost.textContent = "Sending test...";
    try {
      const result = await this._call("test_delivery", payload);
      resultHost.className = result.failed_channels.length ? "test-result hint" : "test-result success";
      resultHost.textContent = result.failed_channels.length ? `Failed: ${result.failed_channels.join(", ")}` : `Success: ${result.succeeded_channels.join(", ")}`;
    } catch (error) { resultHost.className = "test-result hint"; resultHost.textContent = error.message || String(error); }
  }

  _userSelect(selected) {
    const select = document.createElement("select");
    for (const user of this._users) select.add(new Option(user.name, user.id, false, user.id === selected));
    return select;
  }

  _entitySelect(domain, selected = []) {
    const select = document.createElement("select"); select.multiple = true;
    for (const [id, state] of Object.entries(this._hass.states || {}).filter(([id]) => id.startsWith(`${domain}.`))) select.add(new Option(state.attributes.friendly_name || id, id, false, selected.includes(id)));
    return select;
  }

  _selected(select) { return select ? [...select.selectedOptions].map((option) => option.value) : []; }
  _capitalize(value) { return value[0].toUpperCase() + value.slice(1); }
  _formatDate(value) {
    try { return new Intl.DateTimeFormat(this._hass.locale?.language, { dateStyle: "medium", timeStyle: "short", timeZone: this._hass.config.time_zone }).format(new Date(value)); }
    catch { return value; }
  }
  _showError(error) {
    const host = this.shadowRoot?.querySelector("#error"); if (!host) return;
    host.textContent = error?.message || String(error); host.classList.add("show"); host.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  _clearError() { const host = this.shadowRoot?.querySelector("#error"); if (host) { host.textContent = ""; host.classList.remove("show"); } }
}

if (!customElements.get("reminders-management-panel")) customElements.define("reminders-management-panel", RemindersManagementPanel);
