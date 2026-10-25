export const WEEKDAYS = [
  "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
];

const DOMAIN_STATES = {
  alarm_control_panel: ["disarmed", "armed_home", "armed_away", "armed_night", "armed_vacation", "armed_custom_bypass", "pending", "arming", "disarming", "triggered"],
  automation: ["on", "off"],
  binary_sensor: ["on", "off"],
  button: [],
  climate: ["off", "heat", "cool", "heat_cool", "auto", "dry", "fan_only"],
  cover: ["open", "closed", "opening", "closing"],
  device_tracker: ["home", "not_home"],
  fan: ["on", "off"],
  humidifier: ["on", "off"],
  input_boolean: ["on", "off"],
  lawn_mower: ["docked", "mowing", "paused", "returning", "error"],
  light: ["on", "off"],
  lock: ["locked", "unlocked", "locking", "unlocking", "jammed", "opening", "open"],
  media_player: ["off", "on", "idle", "playing", "paused", "standby", "buffering"],
  person: ["home", "not_home"],
  remote: ["on", "off"],
  siren: ["on", "off"],
  sun: ["above_horizon", "below_horizon"],
  switch: ["on", "off"],
  update: ["on", "off"],
  vacuum: ["cleaning", "docked", "paused", "idle", "returning", "error"],
  valve: ["open", "closed", "opening", "closing"],
  water_heater: ["off", "eco", "electric", "gas", "heat_pump", "high_demand", "performance"],
};

export function suggestedStates(states = {}, entityId = "") {
  const stateObj = states[entityId];
  if (!stateObj) return [];
  const domain = entityId.split(".")[0];
  const suggestions = new Set(DOMAIN_STATES[domain] || []);
  for (const value of stateObj.attributes?.options || []) suggestions.add(String(value));
  for (const value of stateObj.attributes?.hvac_modes || []) suggestions.add(String(value));
  for (const [id, item] of Object.entries(states)) {
    if (id.startsWith(`${domain}.`) && !["unknown", "unavailable"].includes(item.state)) suggestions.add(item.state);
  }
  if (!["unknown", "unavailable"].includes(stateObj.state)) suggestions.add(stateObj.state);
  return [...suggestions];
}

const ordinal = (day) => {
  const mod100 = day % 100;
  if (mod100 >= 11 && mod100 <= 13) return `${day}th`;
  return `${day}${({ 1: "st", 2: "nd", 3: "rd" })[day % 10] || "th"}`;
};

export function recurrenceSummary(reminder, locale = undefined) {
  const rule = reminder?.recurrence;
  if (!rule) return "One-time";
  const time = String(rule.anchor_local).slice(11, 16);
  const every = rule.interval === 1 ? "Every" : `Every ${rule.interval}`;
  if (rule.frequency === "daily") {
    return rule.interval === 1 ? `Every day at ${time}` : `${every} days at ${time}`;
  }
  if (rule.frequency === "weekly") {
    const names = rule.weekdays.map((day) => day[0].toUpperCase() + day.slice(1));
    const days = new Intl.ListFormat(locale, { style: "long", type: "conjunction" }).format(names);
    return rule.interval === 1
      ? `Every ${days} at ${time}`
      : `${every} weeks on ${days} at ${time}`;
  }
  if (rule.frequency === "yearly") {
    const date = new Intl.DateTimeFormat(locale, { month: "long", day: "numeric", timeZone: "UTC" })
      .format(new Date(`${String(rule.anchor_local).slice(0, 10)}T12:00:00Z`));
    return rule.interval === 1
      ? `Every year on ${date} at ${time}`
      : `${every} years on ${date} at ${time}`;
  }
  const dayName = rule.monthly_weekday
    ? rule.monthly_weekday[0].toUpperCase() + rule.monthly_weekday.slice(1)
    : "";
  let pattern;
  if (rule.monthly_mode === "last_day") pattern = "the last day";
  else if (rule.monthly_mode === "last_weekday") pattern = `the last ${dayName}`;
  else if (rule.monthly_mode === "nth_weekday") pattern = `the ${ordinal(rule.monthly_week)} ${dayName}`;
  else pattern = `the ${ordinal(rule.day_of_month)}`;
  return rule.interval === 1
    ? `Every month on ${pattern} at ${time}`
    : `${every} months on ${pattern} at ${time}`;
}

export function deliverySummary(reminder, preferences = null) {
  const policy = reminder?.delivery_policy || preferences?.default_delivery_policy;
  if (!policy) return "Use my defaults";
  const labels = { phone: "Phone", voice: "Voice", persistent_notification: "Home Assistant alert" };
  return policy.channels.map((channel) => labels[channel] || channel).join(" + ");
}

export function localInputParts(value) {
  const date = value ? new Date(value) : new Date(Date.now() + 60 * 60 * 1000);
  const pad = (part) => String(part).padStart(2, "0");
  return {
    date: `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`,
    time: `${pad(date.getHours())}:${pad(date.getMinutes())}`,
  };
}

export function zonedInputParts(value, timeZone, locale = "en-CA") {
  const date = value ? new Date(value) : new Date(Date.now() + 60 * 60 * 1000);
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat(locale, {
      timeZone, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(date).map(({ type, value: part }) => [type, part]),
  );
  return { date: `${parts.year}-${parts.month}-${parts.day}`, time: `${parts.hour}:${parts.minute}` };
}

export function localDateTime(date, time) {
  return `${date}T${time}:00`;
}
/** Format an HA-local date/time without interpreting it in the browser timezone.
 *  The backend resolves local wall times in the selected HA/recurrence zone.
 */
export function scheduledLocalSummary(date, time, timeZone, locale = undefined) {
  if (!date || !time) return "";
  const [year, month, day] = date.split("-").map(Number);
  const displayDate = new Intl.DateTimeFormat(locale, {
    dateStyle: "medium", timeZone: "UTC",
  }).format(new Date(Date.UTC(year, month - 1, day, 12)));
  return `on ${displayDate} at ${time} (${timeZone})`;
}


export function quickTimeParts(choice, timeZone, now = Date.now()) {
  const instant = new Date(now);
  if (["10m", "30m", "1h"].includes(choice)) {
    const minutes = { "10m": 10, "30m": 30, "1h": 60 }[choice];
    return zonedInputParts(new Date(instant.getTime() + minutes * 60000), timeZone);
  }
  const today = zonedInputParts(instant, timeZone);
  if (choice === "later") {
    const currentHour = Number(today.time.slice(0, 2));
    // Do not silently offer 23:59 when it is already too late to choose
    // a meaningful "later today" time. The panel disables this shortcut.
    if (currentHour >= 23) return null;
    const hour = Math.min(23, Math.max(18, currentHour + 2));
    return { date: today.date, time: `${String(hour).padStart(2, "0")}:00` };
  }
  const [year, month, day] = today.date.split("-").map(Number);
  // Advance the calendar date, not the instant: +36h can skip tomorrow
  // when the shortcut is used in the afternoon or evening.
  const nextDate = new Date(Date.UTC(year, month - 1, day + 1));
  return { date: nextDate.toISOString().slice(0, 10), time: "09:00" };
}

export function acknowledgementSummary(reminder, preferences = null) {
  if ((reminder?.acknowledgement_policy || "default") === "default"
      && preferences?.require_acknowledgement !== undefined) {
    return preferences.require_acknowledgement
      ? "Keep active until dismissed" : "Dismissal not required";
  }
  return {
    default: "Use my default",
    required: "Keep active until dismissed",
    not_required: "Dismissal not required",
  }[reminder?.acknowledgement_policy || "default"];
}

export function triggerRepeatSummary(reminder) {
  return {
    once: "Once",
    every_trigger: "Every time it happens",
    rearm_after_acknowledgement: "Trigger again after dismissal",
  }[reminder?.repeat_policy || "once"];
}

export function awaitingOccurrences(reminder) {
  return (reminder?.occurrence_history || []).filter(
    (item) => item.status === "awaiting_acknowledgement",
  );
}

const TIMED_SNOOZE_STATUSES = new Set([
  "pending", "waiting_for_context", "awaiting_acknowledgement", "delivered", "failed",
]);
const TRIGGER_SNOOZE_BLOCKED_STATUSES = new Set(["delivering", "expired", "cancelled"]);

export function canSnooze(reminder) {
  if (!reminder || reminder.paused) return false;
  if (reminder.activation_type === "trigger") {
    return !TRIGGER_SNOOZE_BLOCKED_STATUSES.has(reminder.status);
  }
  return TIMED_SNOOZE_STATUSES.has(reminder.status);
}

/** Group already-delivered actionable reminders apart from not-yet-delivered overdue work. */
export function upcomingSection(reminder, timeZone, now = Date.now()) {
  if (reminder.status === "waiting_for_context") return "Waiting for conditions";
  if (reminder.status === "awaiting_acknowledgement") return "Awaiting dismissal";
  if (reminder.status === "delivered") return "Action available";
  if (reminder.activation_type === "time" && reminder.due) {
    return upcomingBucket(reminder.due, timeZone, now);
  }
  return null;
}

export function upcomingBucket(value, timeZone, now = Date.now()) {
  if (new Date(value).getTime() < now) return "Overdue";
  const local = zonedInputParts(value, timeZone).date;
  const today = zonedInputParts(now, timeZone).date;
  const dayNumber = (text) => Date.parse(`${text}T12:00:00Z`) / 86400000;
  const difference = dayNumber(local) - dayNumber(today);
  if (difference <= 0) return "Today";
  if (difference === 1) return "Tomorrow";
  if (difference <= 7) return "This week";
  return "Later";
}

export function relativeTime(value, locale = undefined, now = Date.now()) {
  const seconds = (new Date(value).getTime() - now) / 1000;
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: "always" });
  if (Math.abs(seconds) < 3600) return formatter.format(Math.round(seconds / 60), "minute");
  if (Math.abs(seconds) < 86400) return formatter.format(Math.round(seconds / 3600), "hour");
  return formatter.format(Math.round(seconds / 86400), "day");
}

/** A history row must distinguish "not attempted" from a failed send. */
export function historyDeliverySummary(occurrence) {
  const succeeded = occurrence?.succeeded_channels || [];
  const failed = occurrence?.failed_channels || [];
  if (succeeded.length) return `Delivered by ${succeeded.join(", ")}`;
  if (failed.length || occurrence?.status === "failed") return "Delivery failed";
  if (occurrence?.delivered_at) return "Delivered (channels not recorded)";
  if (occurrence?.status === "completed") return "Completed without notification";
  if (occurrence?.status === "acknowledged") return "Dismissed (delivery not recorded)";
  if (occurrence?.status === "expired") return "Expired without notification";
  if (occurrence?.status === "skipped") return "Skipped without notification";
  if (occurrence?.status === "cancelled") return "Cancelled without notification";
  if (occurrence?.status === "waiting_for_context") return "Waiting for delivery conditions";
  if (occurrence?.status === "scheduled") return "Not yet delivered";
  if (occurrence?.status === "delivering") return "Delivery in progress";
  return "No notification recorded";
}

export function historyHighlights(occurrence, formatDate) {
  const result = [];
  const display = (value) => formatDate(value);
  if (occurrence.scheduled_due && occurrence.due
      && occurrence.scheduled_due !== occurrence.due) {
    result.push(`Moved to ${display(occurrence.due)}`);
  }
  if (occurrence.delivered_at) result.push(`Sent ${display(occurrence.delivered_at)}`);
  if (occurrence.acknowledged_at) result.push(`Dismissed ${display(occurrence.acknowledged_at)}`);
  else if (occurrence.completed_at && occurrence.status === "completed") {
    result.push(`Completed ${display(occurrence.completed_at)}`);
  }
  if (occurrence.next_escalation_at) result.push(`Next follow-up ${display(occurrence.next_escalation_at)}`);
  if (occurrence.completion_reason === "home_assistant_offline") {
    result.push("Missed while Home Assistant was offline");
  } else if (occurrence.completion_reason === "permission_denied") {
    result.push("Skipped because required entity access was revoked");
  } else if (occurrence.completion_reason === "context_wait_expired") {
    result.push("Delivery conditions were not met before expiry");
  }
  if (occurrence.delivery_errors?.length) {
    result.push(`Delivery errors: ${occurrence.delivery_errors.join(", ")}`);
  }
  return result;
}

/** Explain the next user-visible step without changing reminder lifecycle state. */
export function reminderNextStep(reminder, formatDate = (value) => value) {
  if (!reminder) return "";
  if (reminder.status === "waiting_for_context") {
    const active = (reminder.occurrence_history || []).find(
      (item) => item.id === reminder.current_occurrence_id
    );
    const condition = reminder.deliver_when_summary
      || (reminder.delivery_triggers?.length ? "a Home Assistant trigger"
        : reminder.delivery_conditions?.length ? "the selected Home Assistant conditions"
          : "its delivery condition");
    const expiry = active?.expires_at
      ? ` If it is still waiting, it expires ${formatDate(active.expires_at)}.`
      : "";
    return `Waiting for delivery conditions: ${condition}.${expiry}`;
  }
  if (reminder.status === "pending"
      && (reminder.occurrence_history || []).some((item) => item.status === "awaiting_acknowledgement")) {
    return "An earlier occurrence still needs dismissal; the next one remains scheduled.";
  }
  if (reminder.status === "awaiting_acknowledgement") {
    return "Delivered — waiting for you to dismiss this occurrence.";
  }
  if (reminder.status === "failed") {
    return "Delivery failed — review the delivery error or retry the reminder.";
  }
  if (reminder.status === "paused") {
    return "This series is paused and will not deliver until resumed.";
  }
  if (reminder.status === "delivered" && reminder.allow_manual_completion) {
    return "Delivered — mark Done when you complete the task.";
  }
  return "";
}

/** Return the one occurrence that the panel can mark Done before delivery. */
export function currentCompletableOccurrence(reminder) {
  if (!reminder || !reminder.allow_manual_completion || reminder.paused
      || reminder.activation_type !== "time" || reminder.status !== "pending") return null;
  return (reminder.occurrence_history || []).find(
    (item) => item.id === reminder.current_occurrence_id && item.status === "scheduled"
  ) || null;
}
