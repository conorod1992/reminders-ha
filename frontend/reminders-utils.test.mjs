import test from "node:test";
import assert from "node:assert/strict";
import { deliverySummary, localDateTime, recurrenceSummary } from "../custom_components/reminders/frontend/reminders-utils.js";

test("formats supported recurrence rules", () => {
  assert.equal(recurrenceSummary({ recurrence: { frequency: "daily", interval: 1, anchor_local: "2026-08-01T08:00:00" } }), "Every day at 08:00");
  assert.equal(recurrenceSummary({ recurrence: { frequency: "weekly", interval: 2, anchor_local: "2026-08-04T09:00:00", weekdays: ["tuesday", "thursday"] } }, "en"), "Every 2 weeks on Tuesday and Thursday at 09:00");
  assert.equal(recurrenceSummary({ recurrence: { frequency: "monthly", interval: 1, anchor_local: "2026-08-31T12:00:00", day_of_month: 31 } }), "Every month on the 31st at 12:00");
});

test("formats default and custom delivery", () => {
  assert.equal(deliverySummary({ delivery_policy: null }), "Use my defaults");
  assert.equal(deliverySummary({ delivery_policy: { channels: ["phone", "voice"] } }), "Phone + Voice");
});

test("combines date and time without inventing a timezone", () => {
  assert.equal(localDateTime("2026-08-04", "09:30"), "2026-08-04T09:30:00");
});
