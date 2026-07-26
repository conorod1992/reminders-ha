# Reminders for Home Assistant

Reminders is a HACS-compatible custom integration that adds persistent,
multi-user reminders to Home Assistant. Reminders survive restarts, wake at the
next exact due instant rather than polling, and resolve each user's current
delivery preferences when they fire.

## Highlights

- Home Assistant `Store` persistence with a versioned, migration-ready schema
- one scheduler callback for the next due timestamp, including simultaneous due items
- overdue delivery after restart and recovery of interrupted deliveries
- anchored daily, weekly, and monthly recurrences with local-time DST semantics
- ownership by Home Assistant user ID with caller-aware access checks
- per-user defaults and per-reminder custom delivery policies
- persistent notification, modern notify entity, and selected Assist satellite channels
- create, get, list, update, delete, snooze, and preference actions
- privacy-safe diagnostics (counts only; never reminder content or user IDs)

## Installation

### HACS custom repository

1. In HACS, add `https://github.com/conorod1992/reminders-ha` as a custom
   repository with category **Integration**.
2. Install **Reminders**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration**, search for
   **Reminders**, and complete the one-step setup.

### Manual

Copy `custom_components/reminders` into the matching directory below your Home
Assistant configuration directory, restart Home Assistant, and add the
integration from the UI.

Requires Home Assistant 2026.7 or newer.

## Delivery preferences

Run `reminders.set_user_preferences` from Developer Tools → Actions. An
authenticated caller defaults to their own Home Assistant user. Administrators
may select another user. Actions invoked without an authenticated user context
(some automations) must provide `user_id` explicitly.

```yaml
action: reminders.set_user_preferences
data:
  channels:
    - phone
    - persistent_notification
  notify_targets:
    - notify.conors_phone
```

The logical channels are:

- `persistent_notification`: built-in, reliable delivery with no selected endpoint
- `phone`: calls `notify.send_message` for the selected notify entities
- `voice`: calls `assist_satellite.announce` for selected compatible satellites

No device-name inference is used. A reminder with default delivery stores no
copy of these preferences, so changing a target updates every future default
reminder automatically.

## Actions

### Create a one-shot reminder

```yaml
action: reminders.create
data:
  title: Put the bins out
  message: Put the recycling bin at the kerb
  due: "2026-07-27 20:00:00"
```

Naive date/time values from the UI are interpreted in Home Assistant's configured
timezone and stored as UTC. Offset-aware values are also accepted. Set
`response_variable` to capture the new reminder object and ID.

Use a per-reminder override by selecting custom delivery:

```yaml
action: reminders.create
data:
  title: Quiet reminder
  due: "2026-07-27T20:00:00+01:00"
  delivery_mode: custom
  channels:
    - persistent_notification
```

### Create a recurring reminder

`reminders.create_recurring` keeps recurrence fields out of ordinary one-shot
creation. Every series requires an explicit **First reminder**:

```yaml
action: reminders.create_recurring
data:
  title: Put recycling out
  first_reminder: "2026-08-03 20:00:00"
  frequency: weekly
  interval: 2
  weekdays:
    - monday
```

The first reminder date/time defines the recurrence phase. The integration does
not choose which alternating week/month a recurring reminder belongs to. A
past first reminder is retained as the anchor, but creation schedules only the
next future occurrence rather than replaying the history.

Supported patterns are:

- `daily`: every day or every X days at the first reminder's wall-clock time
- `weekly`: every X active weeks on one or more selected weekdays
- `monthly`: every X active months on a selected calendar day

For a single weekly weekday, `weekdays` may be omitted and defaults to the first
reminder's weekday. Multiple weekdays belong to the same active week:

```yaml
action: reminders.create_recurring
data:
  title: Water plants
  first_reminder: "2026-08-04 09:00:00"
  frequency: weekly
  interval: 2
  weekdays:
    - tuesday
    - thursday
```

This produces Tuesday and Thursday in the week beginning 3 August, skips the
following week, then produces Tuesday and Thursday in the week beginning
17 August. The interval is not applied independently to each weekday.

"Every 3 weeks on Friday" means Friday in every third active week starting
from the first reminder's recurrence phase. It does not mean the third Friday
of every month.

For monthly schedules, the first reminder's date must match `day_of_month`.
Months without that calendar day are skipped: a monthly reminder on the 31st
runs on 31 January, then 31 March, not 28 February or 30 April.

The timezone defaults to Home Assistant's configured timezone and can be
overridden with an IANA name such as `Europe/Dublin`. Recurrence stores local
wall time and calculates each UTC instant from the original anchor, so DST does
not shift an 08:00 reminder to 07:00 or 09:00 local time. A nonexistent local
time during the spring gap moves to the first valid wall-clock second after the
gap. An autumn repeated time uses its first occurrence.

### Get and list

These read-only actions require response data:

```yaml
action: reminders.list
data:
  pending_only: true
response_variable: my_reminders
```

`list` supports `user_id`, `pending_only`, `due_after`, and `due_before`.
Ordinary users can only read their own records; administrators may list all or
select another user. Responses retain all existing fields and add `recurring`,
the recurrence definition, its underlying `scheduled_due`, and last-occurrence
details.

### Update, delete, and snooze

All use the UUID returned by `create`, `create_recurring`, or `list`:

```yaml
action: reminders.snooze
data:
  reminder_id: 05d7c355-f394-40d6-b052-d5da1fc979cb
  duration:
    minutes: 30
```

`update` accepts a new title, message, due time, recipient (administrator only),
or delivery policy. For recurring reminders it also accepts `first_reminder`,
`frequency`, `interval`, `weekdays`, `day_of_month`, and `timezone`; changing
any recurrence field recalculates and immediately persists the next occurrence.
Direct `due` edits are rejected for recurring reminders because due is derived
from the recurrence rule. `delete` removes the whole series.

Snoozing a recurring reminder delays only its current occurrence. The stored
regular occurrence remains unchanged, so snoozing this Monday from 20:00 to
21:00 does not move next Monday away from 20:00. Repeated snoozes and restarts
preserve that distinction.

## Restart and failure behavior

At startup, overdue pending reminders are delivered immediately. Before calling
delivery providers, the manager persists a transient `delivering` state. If Home
Assistant stops during delivery, that state is recovered to pending and retried
on the next start. This gives at-least-once recovery: a crash after an endpoint
accepted a message but before final state was saved can produce a duplicate, but
a reminder is not silently lost.

Reminder record mutations use a prepare, persist, then commit sequence under the
manager lock. If an immediate Store write fails, the proposed runtime state is
discarded and the existing reminder records and scheduler remain unchanged. A
delivery-result write failure is slightly different because its `delivering`
claim was already persisted before the provider was called: both storage and
runtime retain that claim, and startup recovery returns it to pending for an
at-least-once retry.

For a recurring series, a long outage produces at most one overdue delivery.
After that representative missed occurrence, the manager calculates the first
future occurrence directly from the original recurrence phase. It never emits
one notification for every missed day/week/month and never derives the next
time from the late delivery timestamp.

Provider failures are isolated. If at least one selected channel succeeds, the
occurrence is successful and its failed channel names are retained. A failed
one-shot reminder is marked failed and is not retried in a tight loop. A failed
recurring occurrence records `last_occurrence_status: failed`, advances to the
next phased occurrence, and leaves the series pending.

## Architecture

`ReminderManager` owns the in-memory records, mutations, persistence snapshots,
and the single next-due callback. Delivery is delegated to providers through
logical `DeliveryPolicy` values. Home Assistant actions and future conversation
tools call the manager rather than accessing storage or physical endpoints.

Storage writes occur only on persistent changes. Reminder creation, update,
deletion, snooze, recurrence advancement, and delivery claim/result transitions
await `Store.async_save` before their action returns or the next schedule is
installed. The candidate reminder dictionary is committed to runtime only after
that save succeeds. Only lower-risk user preference writes remain intentionally
eventually durable through a coalesced delayed save. No reminder entities,
per-reminder automations, timers, minute polling, Recorder tables, or external
databases are created.

## Current limitations and roadmap

- Voice delivery requires satellites that implement `assist_satellite.announce`;
  endpoint capabilities vary, so persistent/phone delivery remains the baseline.
- Recurrence supports daily, active-week weekday, and fixed calendar-day monthly
  rules. It does not yet support RRULE import, yearly rules, "last weekday",
  "third Friday of the month", or "last day of month" shortcuts.
- V1 does not include acknowledgement buttons, retries/escalation, history
  retention controls, presence-aware satellite choice, a dedicated reminder
  dashboard, or direct conversation-agent tools.
- Failed delivery is recorded but has no automatic retry policy in V1.
- User preferences are configured with an action rather than a dedicated profile UI.

The stored model and delivery boundary deliberately leave room for recurrence
with local-time semantics, acknowledgement and escalation state, richer voice
selection, and structured conversation tools.

## Development

```text
python -m pip install -r requirements-test.txt
python -m ruff format --check .
python -m ruff check .
python -m mypy custom_components/reminders
python -m pytest
```

CI additionally runs Hassfest and HACS validation. See [LICENSE](LICENSE).
