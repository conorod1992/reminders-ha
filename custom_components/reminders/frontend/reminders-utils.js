export const WEEKDAYS = [
  "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
];

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

export function deliverySummary(reminder) {
  const policy = reminder?.delivery_policy;
  if (!policy) return "Use my defaults";
  const labels = { phone: "Phone", voice: "Voice", persistent_notification: "Persistent notification" };
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

export function quickTimeParts(choice, timeZone, now = Date.now()) {
  const instant = new Date(now);
  if (["10m", "30m", "1h"].includes(choice)) {
    const minutes = { "10m": 10, "30m": 30, "1h": 60 }[choice];
    return zonedInputParts(new Date(instant.getTime() + minutes * 60000), timeZone);
  }
  const today = zonedInputParts(instant, timeZone);
  if (choice === "later") {
    const currentHour = Number(today.time.slice(0, 2));
    const hour = Math.min(23, Math.max(18, currentHour + 2));
    return { date: today.date, time: `${String(hour).padStart(2, "0")}:${hour === 23 ? "59" : "00"}` };
  }
  const tomorrow = zonedInputParts(new Date(instant.getTime() + 36 * 3600000), timeZone);
  return { date: tomorrow.date, time: "09:00" };
}

export function acknowledgementSummary(reminder) {
  return {
    default: "Use my default",
    required: "Done required",
    not_required: "No completion needed",
  }[reminder?.acknowledgement_policy || "default"];
}

export function awaitingOccurrences(reminder) {
  return (reminder?.occurrence_history || []).filter(
    (item) => item.status === "awaiting_acknowledgement",
  );
}
