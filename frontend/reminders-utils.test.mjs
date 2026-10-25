import test from "node:test";
import assert from "node:assert/strict";
import {
  acknowledgementSummary,
  awaitingOccurrences,
  canSnooze,
  currentCompletableOccurrence,
  deliverySummary,
  historyDeliverySummary,
  historyHighlights,
  outstandingOccurrences,
  localDateTime,
  quickTimeParts,
  recurrenceSummary,
  reminderNextStep,
  scheduledLocalSummary,
  suggestedStates,
  triggerRepeatSummary,
  upcomingBucket,
  upcomingSection,
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
  assert.equal(deliverySummary({ delivery_policy: null }, { default_delivery_policy: { channels: ["phone", "voice"] } }), "Phone + Voice");
  assert.equal(deliverySummary({ delivery_policy: { channels: ["phone", "voice"] } }), "Phone + Voice");
  assert.equal(deliverySummary({ delivery_policy: { channels: ["persistent_notification"] } }), "Home Assistant alert");
});

test("combines date and time without inventing a timezone", () => {
  assert.equal(localDateTime("2026-08-04", "09:30"), "2026-08-04T09:30:00");
});

test("creates quick choices in the configured timezone", () => {
  assert.deepEqual(quickTimeParts("10m", "UTC", Date.parse("2026-08-04T09:00:00Z")), { date: "2026-08-04", time: "09:10" });
  assert.deepEqual(quickTimeParts("tomorrow", "UTC", Date.parse("2026-08-04T09:00:00Z")), { date: "2026-08-05", time: "09:00" });
  assert.deepEqual(quickTimeParts("tomorrow", "Europe/Dublin", Date.parse("2026-10-09T22:30:00Z")), { date: "2026-10-10", time: "09:00" });
  assert.deepEqual(quickTimeParts("tomorrow", "Europe/Dublin", Date.parse("2026-10-09T14:00:00Z")), { date: "2026-10-10", time: "09:00" });
  assert.deepEqual(quickTimeParts("tomorrow", "UTC", Date.parse("2026-12-31T22:00:00Z")), { date: "2027-01-01", time: "09:00" });
});

test("summarizes acknowledgement and finds actionable occurrences", () => {
  assert.equal(acknowledgementSummary({ acknowledgement_policy: "required" }), "Keep active until dismissed");
  assert.equal(acknowledgementSummary({ acknowledgement_policy: "default" }, { require_acknowledgement: false }), "Dismissal not required");
  assert.equal(acknowledgementSummary({ acknowledgement_policy: "default" }, { require_acknowledgement: true }), "Keep active until dismissed");
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
  assert.equal(upcomingBucket("2026-08-26T21:45:00Z", "Europe/Dublin", now), "Overdue");
  assert.equal(upcomingBucket("2026-08-25T09:00:00Z", "Europe/Dublin", now), "Overdue");
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

test("history highlights distinguish planned, actual and resolved times", () => {
  const highlights = historyHighlights({
    scheduled_due: "2026-10-10T17:00:00Z",
    due: "2026-10-10T18:00:00Z",
    delivered_at: "2026-10-10T18:01:00Z",
    completed_at: "2026-10-10T18:04:00Z",
    status: "completed",
    delivery_errors: ["phone: TimeoutError"],
  }, (time) => time.slice(11, 16));
  assert.deepEqual(highlights, [
    "Moved to 18:00",
    "Sent 18:01",
    "Completed 18:04",
    "Delivery errors: phone: TimeoutError",
  ]);
});

test("history highlights tolerate sparse older occurrence records", () => {
  assert.deepEqual(historyHighlights({}, (time) => time), []);
});

test("later-today shortcut never chooses an elapsed or invalid time", () => {
  assert.deepEqual(quickTimeParts("later", "UTC", Date.parse("2026-08-04T09:00:00Z")), {
    date: "2026-08-04", time: "18:00",
  });
  assert.deepEqual(quickTimeParts("later", "UTC", Date.parse("2026-08-04T22:30:00Z")), {
    date: "2026-08-04", time: "23:00",
  });
  assert.equal(quickTimeParts("later", "UTC", Date.parse("2026-08-04T23:00:00Z")), null);
  assert.equal(quickTimeParts("later", "UTC", Date.parse("2026-08-04T23:59:59Z")), null);
  // 22:30 UTC is 23:30 in Dublin during summer time.
  assert.equal(quickTimeParts("later", "Europe/Dublin", Date.parse("2026-08-04T22:30:00Z")), null);
});

test("explains context waits, deadlines and reminder resolution without losing privacy", () => {
  const waiting = {
    status: "waiting_for_context",
    current_occurrence_id: "due-1",
    deliver_when_summary: "when the house is occupied",
    occurrence_history: [
      { id: "other", expires_at: "2026-08-04T15:00:00Z" },
      { id: "due-1", status: "waiting_for_context", expires_at: "2026-08-04T21:00:00Z" },
    ],
  };
  assert.equal(
    reminderNextStep(waiting, (value) => value.slice(11, 16)),
    "Waiting for delivery conditions: when the house is occupied. If it is still waiting, it expires 21:00.",
  );
  assert.equal(reminderNextStep({ status: "failed" }), "Delivery failed — review the delivery error or retry the reminder.");
  assert.equal(reminderNextStep({ status: "awaiting_acknowledgement" }), "Delivered — waiting for you to dismiss this occurrence.");
  assert.equal(reminderNextStep({ status: "pending" }), "");
});

test("pre-delivery Done now requires the active, scheduled, eligible occurrence", () => {
  const reminder = {
    status: "pending",
    activation_type: "time",
    allow_manual_completion: true,
    paused: false,
    current_occurrence_id: "active",
    occurrence_history: [
      { id: "older", status: "scheduled" },
      { id: "active", status: "scheduled" },
    ],
  };
  assert.equal(currentCompletableOccurrence(reminder)?.id, "active");
  assert.equal(currentCompletableOccurrence({ ...reminder, paused: true }), null);
  assert.equal(currentCompletableOccurrence({ ...reminder, allow_manual_completion: false }), null);
  assert.equal(currentCompletableOccurrence({ ...reminder, status: "completed" }), null);
  assert.equal(currentCompletableOccurrence({ ...reminder, activation_type: "trigger" }), null);
  assert.equal(currentCompletableOccurrence({
    ...reminder, occurrence_history: [{ id: "active", status: "completed" }],
  }), null);
});

test("an advanced recurring series explains a prior unresolved occurrence", () => {
  const text = reminderNextStep({
    status: "pending",
    occurrence_history: [{ id: "older", status: "awaiting_acknowledgement" }],
  });
  assert.match(text, /earlier occurrence still needs dismissal/);
});

test("upcoming sections do not mislabel delivered action-required reminders as overdue", () => {
  const now = Date.parse("2026-09-22T10:00:00Z");
  const timeZone = "Europe/Dublin";
  const due = "2026-09-21T08:00:00Z";
  const base = { activation_type: "time", due };
  assert.equal(upcomingSection({ ...base, status: "pending" }, timeZone, now), "Overdue");
  assert.equal(upcomingSection({ ...base, status: "waiting_for_context" }, timeZone, now), "Waiting for conditions");
  assert.equal(upcomingSection({ ...base, status: "awaiting_acknowledgement" }, timeZone, now), "Awaiting dismissal");
  assert.equal(upcomingSection({ ...base, status: "delivered" }, timeZone, now), "Action available");
  assert.equal(upcomingSection({
    ...base, status: "pending", due: "2026-09-23T08:00:00Z",
  }, timeZone, now), "Tomorrow");
  assert.equal(upcomingSection({
    activation_type: "trigger", status: "waiting_for_trigger", due: null,
  }, timeZone, now), null);
});

test("schedule preview preserves HA wall time independent of browser timezone", () => {
  assert.equal(
    scheduledLocalSummary("2026-10-25", "09:00", "Europe/Dublin", "en-GB"),
    "on 25 Oct 2026 at 09:00 (Europe/Dublin)",
  );
  assert.ok(scheduledLocalSummary("2026-03-29", "02:30", "Europe/Dublin", "en-GB").includes(
    "02:30 (Europe/Dublin)",
  ));
  assert.ok(scheduledLocalSummary("2026-11-01", "14:00", "America/New_York", "en-US").includes(
    "14:00 (America/New_York)",
  ));
});

test("history does not report failed delivery when no notification was attempted", () => {
  for (const [status, expected] of [
    ["completed", "Completed without notification"],
    ["expired", "Expired without notification"],
    ["skipped", "Skipped without notification"],
    ["cancelled", "Cancelled without notification"],
    ["waiting_for_context", "Waiting for delivery conditions"],
  ]) {
    assert.equal(historyDeliverySummary({ status, succeeded_channels: [], failed_channels: [] }), expected);
  }
  assert.equal(historyDeliverySummary({ status: "failed", failed_channels: ["phone"] }), "Delivery failed");
  assert.equal(historyDeliverySummary({ status: "completed", delivered_at: "2026-10-10T11:00:00Z",
    succeeded_channels: ["phone"], failed_channels: ["voice"] }), "Delivered by phone");
  assert.equal(historyDeliverySummary({ status: "acknowledged", delivered_at: "2026-10-10T11:00:00Z" }),
    "Delivered (channels not recorded)");
});

test("history gives specific reasons for missed, revoked and expired reminders", () => {
  const format = (v) => v;
  assert.deepEqual(historyHighlights({ completion_reason: "home_assistant_offline" }, format),
    ["Missed while Home Assistant was offline"]);
  assert.deepEqual(historyHighlights({ completion_reason: "permission_denied" }, format),
    ["Skipped because required entity access was revoked"]);
  assert.deepEqual(historyHighlights({ completion_reason: "context_wait_expired" }, format),
    ["Delivery conditions were not met before expiry"]);
  assert.deepEqual(historyHighlights({ completion_reason: "done" }, format), []);
});

test("unresolved actions are matched to individual occurrences, with resolved history excluded", () => {
  const reminder = {
    allow_manual_completion: true,
    occurrence_history: [
      { id: "old", status: "awaiting_acknowledgement", scheduled_due: "2026-10-01T10:00:00Z" },
      { id: "done", status: "completed", scheduled_due: "2026-10-02T10:00:00Z" },
      { id: "new", status: "delivered", scheduled_due: "2026-10-03T10:00:00Z" },
      { id: "scheduled", status: "scheduled", scheduled_due: "2026-10-04T10:00:00Z" },
    ],
  };
  assert.deepEqual(outstandingOccurrences(reminder).map((x) => x.id), ["new", "old"]);
  assert.deepEqual(outstandingOccurrences({
    ...reminder, allow_manual_completion: false,
  }).map((x) => x.id), ["old"]);
  assert.deepEqual(outstandingOccurrences({
    ...reminder, occurrence_history: [
      { id: "already-selected", status: "delivered", external_action_id: "done" },
    ],
  }), []);
  assert.deepEqual(outstandingOccurrences(null), []);
});
