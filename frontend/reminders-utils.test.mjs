import test from "node:test";
import assert from "node:assert/strict";
import {
  acknowledgementSummary,
  awaitingOccurrences,
  canSnooze,
  deliverySummary,
  localDateTime,
  quickTimeParts,
  recurrenceSummary,
  suggestedStates,
  triggerRepeatSummary,
  upcomingBucket,
  relativeTime,
} from "../custom_components/reminders/frontend/reminders-utils.js";

test("formats supported recurrence rules", () => {
  assert.equal(recurrenceSummary({ recurrence: { frequency: "daily", interval: 1, anchor_local: "2026-08-01T08:00:00" } }), "Every day at 08:00");
  assert.equal(recurrenceSummary({ recurrence: { frequency: "weekly", interval: 2, anchor_local: "2026-08-04T09:00:00", weekdays: ["tuesday", "thursday"] } }, "en"), "Every 2 weeks on Tuesday and Thursday at 09:00");
  assert.equal(recurrenceSummary({ recurrence: { frequency: "monthly", interval: 1, anchor_local: "2026-08-31T12:00:00", day_of_month: 31 } }), "Every month on the 31st at 12:00");
  assert.equal(recurrenceSummary({ recurrence: { frequency: "monthly", interval: 1, anchor_local: "2026-08-17T09:00:00", monthly_mode: "nth_weekday", monthly_week: 3, monthly_weekday: "monday" } }), "Every month on the 3rd Monday at 09:00");
  assert.equal(recurrenceSummary({ recurrence: { frequency: "monthly", interval: 1, anchor_local: "2026-08-31T09:00:00", monthly_mode: "last_weekday", monthly_weekday: "monday" } }), "Every month on the last Monday at 09:00");
  assert.equal(recurrenceSummary({ recurrence: { frequency: "yearly", interval: 1, anchor_local: "2026-12-25T08:00:00" } }, "en"), "Every year on December 25 at 08:00");
});

test("formats default and custom delivery", () => {
  assert.equal(deliverySummary({ delivery_policy: null }), "Use my defaults");
  assert.equal(deliverySummary({ delivery_policy: { channels: ["phone", "voice"] } }), "Phone + Voice");
});

test("combines date and time without inventing a timezone", () => {
  assert.equal(localDateTime("2026-08-04", "09:30"), "2026-08-04T09:30:00");
});

test("creates quick choices in the configured timezone", () => {
  assert.deepEqual(quickTimeParts("10m", "UTC", Date.parse("2026-08-04T09:00:00Z")), { date: "2026-08-04", time: "09:10" });
  assert.deepEqual(quickTimeParts("tomorrow", "UTC", Date.parse("2026-08-04T09:00:00Z")), { date: "2026-08-05", time: "09:00" });
});

test("summarizes acknowledgement and finds actionable occurrences", () => {
  assert.equal(acknowledgementSummary({ acknowledgement_policy: "required" }), "Keep reminding until dismissed");
  assert.deepEqual(awaitingOccurrences({ occurrence_history: [{ id: "a", status: "awaiting_acknowledgement" }, { id: "b", status: "delivered" }] }).map((item) => item.id), ["a"]);
});

test("uses user-friendly trigger repeat summaries without changing stored values", () => {
  assert.equal(triggerRepeatSummary({ repeat_policy: "once" }), "Once");
  assert.equal(triggerRepeatSummary({ repeat_policy: "every_trigger" }), "Every time it happens");
  assert.equal(triggerRepeatSummary({ repeat_policy: "rearm_after_acknowledgement" }), "Trigger again after dismissal");
});

test("suggests domain and entity-specific states", () => {
  const states = {
    "light.study": { state: "on", attributes: {} },
    "light.kitchen": { state: "off", attributes: {} },
    "select.mode": { state: "home", attributes: { options: ["home", "away"] } },
  };
  assert.deepEqual(suggestedStates(states, "light.study"), ["on", "off"]);
  assert.deepEqual(suggestedStates(states, "select.mode"), ["home", "away"]);
});

test("groups upcoming reminders using the Home Assistant timezone", () => {
  const now = Date.parse("2026-08-26T22:30:00Z");
  assert.equal(upcomingBucket("2026-08-26T22:45:00Z", "Europe/Dublin", now), "Today");
  assert.equal(upcomingBucket("2026-08-27T08:00:00Z", "Europe/Dublin", now), "Tomorrow");
  assert.equal(upcomingBucket("2026-08-31T08:00:00Z", "Europe/Dublin", now), "This week");
  assert.equal(upcomingBucket("2026-09-20T08:00:00Z", "Europe/Dublin", now), "Later");
  assert.equal(relativeTime("2026-08-26T23:12:00Z", "en", now), "in 42 minutes");
});


test("only offers snooze where it can produce a valid retry", () => {
  assert.equal(canSnooze({ activation_type: "time", status: "pending" }), true);
  assert.equal(canSnooze({ activation_type: "time", status: "waiting_for_context" }), true);
  assert.equal(canSnooze({ activation_type: "time", status: "awaiting_acknowledgement" }), true);
  assert.equal(canSnooze({ activation_type: "time", status: "delivered" }), true);
  assert.equal(canSnooze({ activation_type: "time", status: "failed" }), true);
  assert.equal(canSnooze({ activation_type: "time", status: "completed" }), false);
  assert.equal(canSnooze({ activation_type: "time", status: "acknowledged" }), false);
  assert.equal(canSnooze({ activation_type: "time", status: "expired" }), false);
  assert.equal(canSnooze({ activation_type: "time", status: "skipped" }), false);
  assert.equal(canSnooze({ activation_type: "trigger", status: "completed" }), true);
  assert.equal(canSnooze({ activation_type: "trigger", status: "acknowledged" }), true);
  assert.equal(canSnooze({ activation_type: "trigger", status: "expired" }), false);
  assert.equal(canSnooze({ activation_type: "time", status: "pending", paused: true }), false);
});
