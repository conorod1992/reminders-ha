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
  return rule.interval === 1
    ? `Every month on the ${ordinal(rule.day_of_month)} at ${time}`
    : `${every} months on the ${ordinal(rule.day_of_month)} at ${time}`;
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
