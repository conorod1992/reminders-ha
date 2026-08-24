# Reminders for Home Assistant

Reminders is a HACS-compatible, multi-user reminder integration for Home
Assistant. It persists reminders across restarts, wakes at the exact next due
instant, and delivers through Home Assistant notifications, phone notify
entities, and Assist satellites.

It deliberately creates no per-reminder entities, automations, polling loops,
Recorder tables, external databases, or per-occurrence Home Assistant objects.

## Highlights

- beginner create flow: **Title → When → Save**
- quick choices for 10 minutes, 30 minutes, one hour, later today, and tomorrow
  morning, alongside exact date/time controls
- anchored daily, weekly, monthly, and yearly recurrences with local wall-clock
  and deterministic DST behavior
- nth/last weekday, last calendar day, end date, and occurrence-count recurrence
  constraints
- optional per-occurrence Done/acknowledgement tracking
- bounded, searchable occurrence history with channel-level delivery results
- first-run delivery wizard, friendly target names, and delivery test controls
- per-user voice quiet hours with a persistent-notification fallback
- duplicate, edit, snooze, search, filter, and live-update panel controls
- authenticated WebSocket API, visual-editor-friendly actions, and structured
  conversation/LLM tools
- immutable Home Assistant user-ID ownership enforced in the backend
- one exact next-due callback for the whole integration; no polling

## Installation

### HACS custom repository

1. In HACS, add `https://github.com/conorod1992/reminders-ha` as a custom
   repository with category **Integration**.
2. Install **Reminders** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**, search for
   **Reminders**, and complete the one-step setup.
4. Open **Sidebar → Reminders**.

For manual installation, copy `custom_components/reminders` into the matching
Home Assistant configuration directory, restart, and add the integration.

Requires Home Assistant 2026.7 or newer.

## Panel and first-run setup

The first time a user opens Reminders, a dismissible setup dialog explains and
configures:

- Home Assistant persistent notifications (the simple, reliable default)
- phone notify targets
- Assist satellite voice targets
- whether reminders should require Done by default

Entity choices use friendly Home Assistant names; users never need to type
entity IDs. Optional channels may be skipped, and dismissing the wizard does
not block the normal panel. An administrator's first-run dialog is always for
their own user. Administrators can explicitly choose another user only from the
regular Preferences dialog.

Preferences provides separate **Test notification**, **Test phone**, **Test
voice**, and **Test configured delivery** controls. Tests use the targets
currently selected in the form, even before Save, and call the same delivery
providers as real reminders. They send “This is a test reminder from Home
Assistant.” and do not create a reminder or history item.

## Creating and managing reminders

The default Add dialog shows only the title, quick time choices, exact date and
time, and Save. Message, recurrence, recipient, custom delivery,
dismissal, optional manual completion, and urgent quiet-hours override are under
**Advanced options**.

Quick choices populate the normal date/time fields in Home Assistant's
configured timezone. They do not use a separate scheduler. The date/time fields
remain editable before Save.

Each reminder card supports:

- **Edit**
- **Duplicate**
- **Snooze**
- **Done**, only when manual completion is enabled
- **Dismiss**, when a delivered occurrence requires dismissal
- **Delete**

Duplicating opens an unsaved form. A one-time copy deliberately has no due time,
so a new time must be chosen. A recurring copy retains its rule as an editable
new series definition, receives a new ID only when saved, and never copies
history, occurrence state, acknowledgement state, or internal IDs.

Search matches title and message in the backend. Administrators can explicitly
filter by owner using friendly display names while the API continues submitting
immutable IDs. Upcoming, Recurring, History, and Failed views remain live via a
privacy-preserving invalidation subscription; there is no polling.

## Dismissal and optional task completion

Not every reminder needs dismissal tracking. Each user chooses **Keep reminding
until dismissed by default**, and each reminder has one of three policies:

- `default`: use the owner's current preference
- `required`: keep reminding until this occurrence is dismissed
- `not_required`: never require dismissal for this reminder/series

Legacy users and reminders default to `false` and `default`, so upgrading does
not suddenly require acknowledgement.

Like default delivery settings, `default` is resolved at **occurrence delivery
time**, not creation time. Changing the user preference therefore affects
future deliveries, including future occurrences of an existing series, without
rewriting reminder records. The resolved boolean is recorded on that occurrence
so its historical meaning never changes later.

A successful required delivery becomes `awaiting_acknowledgement`; the internal
storage vocabulary is retained for compatibility. **Dismiss** records an
acknowledgement timestamp without claiming task completion. With
`allow_manual_completion: true`, **Done** instead records a distinct `completed`
occurrence, completion timestamp, and authenticated Home Assistant user ID.
For a recurring series, only the delivered occurrence is acknowledged. The
series has already advanced to its next anchored occurrence and continues
normally.

The lifecycle vocabulary is explicit:

- scheduled
- delivering (durable transient claim)
- delivered
- awaiting acknowledgement (shown as awaiting dismissal)
- acknowledged (shown as dismissed)
- completed
- failed
- cancelled where a retained occurrence is replaced by a recurrence edit

## Occurrence history and retention

Each one-time reminder and recurring occurrence records:

- original scheduled due time and current due time
- whether and when it was snoozed
- actual delivery time
- successful, failed, and quiet-hours-suppressed channels
- privacy-safe provider error types
- whether acknowledgement was required
- acknowledgement time and user ID when available
- eventual outcome

History is embedded in the reminder/series Store record; it does not create
entities or Recorder rows. The History view uses backend search, date/status
filters, limits, and offsets, so the browser never needs an unbounded dataset.

Defaults are **90 days** and **250 occurrences per reminder**. Users may adjust
both in Preferences (or the preference action). Retention runs on delivery and
preference changes. The active scheduled occurrence and occurrences awaiting
acknowledgement are protected from pruning; this can temporarily exceed the
count cap if many acknowledgements are outstanding.

Deleting a reminder retains the integration's backwards-compatible hard-delete
semantics and removes its embedded history too.

## Delivery preferences and quiet hours

A reminder set to **Use my defaults** resolves the owner's latest delivery
preferences when the occurrence fires. A custom reminder stores its own logical
channels and selected targets.

Supported channels are:

- `persistent_notification`: built-in Home Assistant notification
- `phone`: ordinary `notify.send_message` to selected notify entities, plus
  optional explicit `notify.mobile_app_*` services for Companion App actions
- `voice`: `assist_satellite.announce` to selected Assist satellites

Quiet hours are per user and follow Home Assistant's configured timezone. The
default window is 23:00–07:00, disabled until the user enables it. Overnight
windows work naturally. By default quiet hours suppress voice, allow existing
phone/persistent delivery to continue, and add persistent notification as a
fallback when a selected voice channel is suppressed.

Quiet hours do **not** defer or move a reminder. The occurrence remains due at
its scheduled instant and its history records suppressed channels. An advanced
per-reminder `ignore` policy bypasses quiet hours for urgent reminders.

## Context-aware delivery, automatic completion, and escalation

Advanced options keep the normal **Title → When → Save** flow unchanged. A
scheduled reminder may additionally define `deliver_when`, `complete_when`, and
an acknowledgement escalation policy. These fields use the same bounded
`state`, `numeric_state`, `zone`, `event`, and `named` trigger definitions as
triggered reminders—never templates, arbitrary services, or condition trees.

`deliver_when` makes the due time an eligibility boundary. State, numeric-state,
and zone contexts that are already matching at that boundary deliver
immediately (except duration conditions, which wait for a future qualifying
transition). Event and named contexts always require a future event. Otherwise
the occurrence is persisted as `waiting_for_context`; it is not considered
overdue and is reconstructed after restart without polling or synthetic events.

```yaml
action: reminders.create
data:
  title: Take the parcel to the car
  due: "2026-08-12 18:00:00"
  deliver_when:
    type: state
    entity_id: person.conor
    to: home
```

`complete_when` resolves the relevant occurrence when Home Assistant observes
that the task happened. Before delivery this cancels that occurrence; while it
is waiting for context it prevents delivery; and while it awaits Done it records
an automatic acknowledgement with no impersonated Home Assistant user. Existing
state at creation or restart is deliberately not treated as proof of completion;
event and named completion always require a future event.

```yaml
action: reminders.create
data:
  title: Brush teeth
  due: "2026-08-12 22:30:00"
  acknowledgement_policy: required
  complete_when:
    type: event
    event_type: personal_activity_event
    event_data:
      type: brushing_started
```

Escalation is opt-in and only runs while a successfully delivered occurrence
still awaits acknowledgement. `max_attempts` counts escalation attempts, not
the original delivery. Each attempt and provider result is retained on that
bounded occurrence record. Done, automatic completion, snooze, edit, or delete
invalidates the pending callback. Provider failure waits for the configured
repeat delay, and each attempt resolves delivery targets and quiet hours again;
only the existing `ignore` quiet-hours policy bypasses quiet hours.

Each escalation attempt is durably claimed before its provider call. Its attempt
number is therefore at most once across interruption: after restart, a claimed
attempt is not called again. A crash after the claim write but before (or during)
the provider call can leave that attempt with an unknown result; the following
configured attempt remains eligible. This deliberately avoids duplicating the
same escalation call after an uncertain external side effect.

```yaml
action: reminders.create
data:
  title: Check the back door
  due: "2026-08-12 23:00:00"
  acknowledgement_policy: required
  escalation:
    initial_delay_minutes: 30
    repeat_minutes: 60
    max_attempts: 3
```

Explicitly selected Companion App `notify.mobile_app_*` services receive
**Done** when manual completion is enabled, **Dismiss** when dismissal is
required, and the existing snooze actions. Generic notify entities receive only
the standard title and message supported by the Notify entity API. If a selected
Companion App service rejects the action payload, delivery retries that same
service once as an ordinary notification without buttons. The integration
listens directly for mobile notification action events; no automation is needed.
Each action uses a random persisted token scoped to the exact occurrence,
contains no reminder or owner data, and is an idempotent no-op after that
occurrence is resolved or deleted.

For recurring reminders, at most one scheduled/context-waiting occurrence is
active. If later anchors pass while it waits, delivery or automatic completion
advances from the anchored rule to the next future occurrence rather than
creating a backlog. Snoozing a context-waiting occurrence moves its next
eligibility check while retaining its original scheduled time in history.
Snoozing an already delivered recurring occurrence does not replace the next
anchored occurrence: the next occurrence remains current and unchanged while
the older occurrence is scheduled as an independent, restart-safe retry.

Hybrid examples include “tomorrow after 18:00, when I get home” by combining
`due` and `deliver_when`, and “at 21:00 unless brushing is detected first” by
combining `due` and `complete_when`. More complex external semantics can remain
a bounded named trigger.

## Recurrence model

All recurrence is anchored to **First reminder** in a named timezone. The anchor
defines the local wall-clock time and the active week/month/year phase. Supported
patterns are:

- daily, every N days
- weekly, every N active weeks on one or more weekdays
- monthly on calendar day N (legacy behavior)
- monthly on the first through fifth weekday
- monthly on the last chosen weekday
- monthly on the last calendar day
- yearly, every N years on the anchor month/day
- optional inclusive local end date
- optional maximum occurrence count from the original anchor

The panel offers Weekdays and Weekends presets, a plain-English summary, and a
backend-calculated preview of upcoming occurrences.

Existing monthly-day rules remain unchanged: months without the chosen day are
skipped, so a rule on the 31st does not move to the 30th or February's last day.
The new `last_day` rule is explicit. A yearly 29 February rule similarly skips
non-leap years.

Next occurrences are calculated from the original phase. Late delivery and
snoozing never introduce drift. After downtime the manager delivers at most one
representative overdue occurrence and calculates the next future occurrence
directly; it does not replay every missed event. Count limits remain anchored,
so missed occurrences still consume their original sequence positions.

For DST, local wall time remains authoritative. An ambiguous autumn time uses
the first occurrence (`fold=0`). A nonexistent spring time moves forward to the
first valid wall-clock second after the gap. These policies are deterministic
and apply to previews and delivery scheduling alike.

## Triggered reminders

Triggered reminders wait for something to happen instead of a date/time. Choose
**When something happens** in Add reminder, select the trigger type, and fill in
the small set of fields shown for it. Advanced options contain repeat policy,
cooldown, availability, expiry, duration matching, and already-matching
behavior. Scheduled, recurring, and triggered reminders all use the same
delivery providers, quiet hours, acknowledgement, snooze, ownership, and
bounded occurrence history.

The integration registers Home Assistant event/state listeners directly. It
does not create automation entities. Equivalent definitions share a listener,
listeners are rebuilt from storage at startup, and unused listeners are removed
after edits, deletion, completion, expiry, or unload.

Supported trigger definitions are:

```yaml
# State (attribute and for_seconds are optional)
type: state
entity_id: sensor.work_status
to: Finished for today

# Numeric state; above and below form an optional bounded range
type: numeric_state
entity_id: sensor.printer_toner
below: 10

# Normal Home Assistant zone entry/exit semantics
type: zone
entity_id: person.conor
zone_entity_id: zone.woodies_carlow
event: enter

# Event-data is a subset match; additional fired-event keys are allowed
type: event
event_type: jarvis_opportunity
event_data:
  type: printing_started

# Integration-owned extension point
type: named
trigger_id: quiet_time_after_work
```

State triggers fire only for a real change in the observed state or selected
attribute. Numeric triggers fire when the value crosses into the requested
range, not on every update while it stays there. Zone triggers use Home
Assistant's zone/location calculation. `unknown`, `unavailable`, missing, and
non-numeric values are handled as non-matches.

`fire_if_already_matching` defaults to `false`. A reminder created or restored
while its state, numeric range, or zone already matches therefore waits for a
future genuine transition. This prevents restart from becoming a trigger. When
enabled, the condition is evaluated once after the reminder is safely
persisted. For a `for_seconds` condition whose prior match duration cannot be
proven, Reminders conservatively waits the full configured duration. Event and
named triggers have no durable already-matching state.

Repeat policies are `once` (the default), `every_trigger`, and
`rearm_after_acknowledgement`. Every-trigger reminders skip by default while an
older occurrence awaits acknowledgement; Advanced options can instead permit a
new occurrence. Rearm-after-acknowledgement always waits for a new transition
after Done. Cooldown is per reminder and starts when an occurrence is durably
created, so provider failure cannot cause rapid duplicate occurrence creation.
Its last activation time persists across restarts.

Before `available_from`, hits are ignored and the reminder remains armed. At
`expires_at`, it becomes an expired reminder-level record without inventing a
fake delivered occurrence, and its listener is removed. These boundaries use
exact callbacks, not polling. Timed snooze suppresses hits until the selected
time and then waits for the next transition. **Wait for next trigger** resolves
the current occurrence and explicitly re-arms even a once reminder.

### Triggered reminder actions

```yaml
action: reminders.create_triggered
data:
  title: Get sealant
  trigger:
    type: zone
    entity_id: person.conor
    zone_entity_id: zone.woodies_carlow
    event: enter
  repeat_policy: once
  fire_if_already_matching: false
```

```yaml
action: reminders.create_triggered
data:
  title: Review tomorrow's schedule
  trigger:
    type: state
    entity_id: sensor.work_status
    to: Finished for today
  repeat_policy: every_trigger
  cooldown_seconds: 21600
```

```yaml
action: reminders.create_triggered
data:
  title: Check the printer toner
  trigger:
    type: named
    trigger_id: printing_started
  repeat_policy: rearm_after_acknowledgement
  acknowledgement_policy: required
```

```yaml
action: reminders.fire_trigger
data:
  trigger_id: printing_started
```

Named triggers let Jarvis or another bounded context engine decide when an
opportunity exists without teaching Reminders that higher-level logic. A normal
automation may optionally bridge an unsupported source into a named trigger:

```yaml
alias: Reminder Trigger - Printing Started
triggers:
  - trigger: state
    entity_id: sensor.hp_printer_status
    to: printing
actions:
  - action: reminders.fire_trigger
    data:
      trigger_id: printing_started
```

That automation is optional and only applies to named triggers. State,
numeric-state, zone, and event reminders are listened for directly.

Named-trigger IDs are normalized to lowercase and restricted to letters,
digits, underscores, dots, and hyphens. Ordinary callers fire and receive
counts only for their own reminders. Administrators must explicitly target
another user, and system-context actions require an explicit valid `user_id`.
Configured event-data snapshots are bounded and retained in history; arbitrary
event payloads, user IDs, reminder text, and targets are excluded from
diagnostics.

The initial trigger version deliberately excludes templates, arbitrary service
execution, device-trigger schemas, webhooks, complex AND/OR condition trees,
custom radius calculations, and automatic automation generation. Use a named
trigger when external logic is more complex than the supported direct types.

## Home Assistant actions

All actions have visual-editor names, descriptions, examples, and selectors.
Home Assistant 2026.7 does not expose a valid service-action user selector, so
admin `user_id` fields use the best supported text selector and clearly explain
that they expect the immutable ID. The custom panel provides the natural
friendly-name user picker. Ordinary users cannot target another user even if
they manually write an ID.

Existing action names remain compatible:

- `reminders.create`
- `reminders.create_recurring`
- `reminders.get`
- `reminders.list`
- `reminders.update`
- `reminders.delete`
- `reminders.snooze`
- `reminders.set_user_preferences`

New actions are:

- `reminders.acknowledge`
- `reminders.test_delivery`

An authenticated action defaults to its caller. A system action without a user
context must explicitly provide a valid `user_id`.

### Create examples

```yaml
action: reminders.create
data:
  title: Put the bins out
  due: "2026-08-04 20:00:00"
  acknowledgement_policy: default
```

```yaml
action: reminders.create_recurring
data:
  title: Monthly report
  first_reminder: "2026-08-03 09:00:00"
  frequency: monthly
  monthly_mode: nth_weekday
  monthly_week: 1
  monthly_weekday: monday
  occurrence_count: 12
```

Naive date/time values use Home Assistant's timezone; offset-aware values are
converted to UTC. Set `response_variable` to capture returned reminder data.

### Acknowledge and test examples

```yaml
action: reminders.acknowledge
data:
  reminder_id: 05d7c355-f394-40d6-b052-d5da1fc979cb
```

For recurring history with multiple outstanding items, also provide
`occurrence_id`.

```yaml
action: reminders.test_delivery
data:
  channels:
    - phone
  notify_targets:
    - notify.conors_phone
response_variable: delivery_test
```

The response lists successful and failed channels and safe error summaries.

### External integration metadata

Other integrations can create and rediscover their reminders without importing
Reminders internals. `create`, `create_recurring`, `create_triggered`, and
`update` accept optional bounded `source`, `source_id`, `source_event`, and
`managed_externally` fields. Use `list` (or `reminders/list`) with `source` and
`source_id` to find them again; normal caller ownership rules still apply.

Externally managed reminders may also provide up to five inert
`external_actions`, each containing only a bounded `id` and `label`. These
actions cannot call services, render templates, open URLs, or execute callbacks.
They can coexist with Snooze and Dismiss; generic Done still requires
`allow_manual_completion: true`.

Each durable dismissal, manual completion, automatic completion, snooze,
external action selection, or deletion fires
the `reminders_lifecycle` Home Assistant event. Its safe payload includes
`action`, `reminder_id`, optional `occurrence_id`, `user_id`, and the source
metadata, but never the reminder title or message. An external selection uses
`action: external_action` and adds `external_action_id`.

An owning integration can therefore set `allow_manual_completion: false`, set
`acknowledgement_policy: required`, provide (for example)
`external_actions: [{"id": "renewed", "label": "Renewed"}]`, and listen for
`reminders_lifecycle` events matching its `source`/`source_id` and
`external_action_id`.

## Structured conversation tools

The integration registers an opt-in **Reminders** LLM API for Home Assistant
conversation agents. Select that API in an agent's configuration to expose:

- create one-time reminder
- create recurring reminder
- list reminders
- get reminder details
- update reminder
- delete/cancel reminder
- snooze reminder
- dismiss an occurrence
- complete a task when enabled
- query history

Tools use structured arguments and return structured reminder/history data.
They call `ReminderManager`; they cannot execute arbitrary services.

When the conversation context contains an authenticated Home Assistant user,
the same ownership checks as the panel and actions apply. An ordinary user can
never read or mutate another user's records. Administrator cross-user behavior
requires an explicit `user_id` argument. Calls with no authenticated user
context are rejected.

Title-based tools require an exact, unique match. If zero or multiple reminders
match, mutation does not run; the tool returns `needs_disambiguation` and safe
candidate IDs/times so the agent can ask a follow-up question.

## Persistence, restart, and migration

Storage schema 1.3 adds recurrence pattern/limit fields, per-user preferences,
and occurrence history. Migration from 1.0–1.2:

- preserves all existing reminder IDs, owners, content, due times, policies,
  recurrence anchors, and legacy monthly-day semantics
- creates one conservative lifecycle occurrence for each legacy reminder
- sets acknowledgement to `default` with the user default `false`
- disables quiet hours until explicitly configured
- marks existing preference records as configured
- applies 90-day/250-occurrence retention defaults

Malformed individual records remain isolated rather than discarding the whole
Store document.

Reminder mutations follow prepare → persist → commit under the manager lock.
Creation, update, delete, snooze, acknowledgement, recurrence advancement, and
delivery claim/result transitions await an atomic Store write before runtime
state is committed. Preference changes are now immediately durable too.

Before provider calls, due work is persisted as `delivering`. A restart recovers
that claim to the pending delivery path. This is intentionally at-least-once: a
crash after an endpoint accepted a message but before the result write may
produce a duplicate, but does not silently lose a reminder.

Provider failures are isolated. If at least one channel succeeds, delivery is
successful and failed channels remain in history. A fully failed one-time
reminder stops without a retry loop. A failed recurring occurrence is recorded
and the series advances to its next anchored occurrence.

## Security and diagnostics

Reminder ownership is an immutable Home Assistant user ID. Every service,
WebSocket command, history query, preference mutation, delivery test, and
conversation tool resolves the authenticated actor in the backend. The panel
is not a security boundary.

Ordinary users can access only their own reminders, preferences, and history.
Administrators may explicitly target another user or request all-user views.
Names and entity IDs are display/endpoint values only and never infer ownership.

Diagnostics expose aggregate counts and scheduler presence only; reminder text,
user IDs, targets, and history content are excluded.

## Deliberate limitations

- Notification actions require an explicitly selected Home Assistant Companion
  App `notify.mobile_app_*` service; generic notify entities remain ordinary.
- Conversation tools are available only to agents whose configuration opts into
  the Reminders LLM API, and authenticated behavior depends on the agent passing
  Home Assistant request context.
- Raw RRULE import/export is not the primary UI and is deferred.
- Failed initial deliveries remain visible without an automatic retry loop;
  acknowledgement escalation is an explicit per-reminder policy.
- Quiet hours use Home Assistant's configured timezone because Home Assistant
  does not currently expose an independent timezone on each user record.
- Home Assistant 2026.7's generic service selector schema rejects `user`, so
  service actions retain an explained `user_id` text field. The custom panel
  supplies the secure friendly-name picker.

## Development

```text
python -m pip install -r requirements-test.txt
python -m ruff format --check .
python -m ruff check .
python -m mypy custom_components/reminders
python -m pytest
cd frontend
npm run build
npm run lint
npm test
```

CI additionally runs Hassfest and HACS validation. See [LICENSE](LICENSE).
