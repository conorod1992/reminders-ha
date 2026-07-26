# Reminders for Home Assistant

Reminders is a HACS-compatible custom integration that adds persistent,
multi-user reminders to Home Assistant. Reminders survive restarts, wake at the
next exact due instant rather than polling, and resolve each user's current
delivery preferences when they fire.

## Highlights

- Home Assistant `Store` persistence with a versioned, migration-ready schema
- one scheduler callback for the next due timestamp, including simultaneous due items
- overdue delivery after restart and recovery of interrupted deliveries
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

### Create

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
select another user.

### Update, delete, and snooze

All use the UUID returned by `create` or `list`:

```yaml
action: reminders.snooze
data:
  reminder_id: 05d7c355-f394-40d6-b052-d5da1fc979cb
  duration:
    minutes: 30
```

`update` accepts a new title, message, due time, recipient (administrator only),
or delivery policy. `delete` permanently removes the record.

## Restart and failure behavior

At startup, overdue pending reminders are delivered immediately. Before calling
delivery providers, the manager persists a transient `delivering` state. If Home
Assistant stops during delivery, that state is recovered to pending and retried
on the next start. This gives at-least-once recovery: a crash after an endpoint
accepted a message but before final state was saved can produce a duplicate, but
a reminder is not silently lost.

Provider failures are isolated. If at least one selected channel succeeds, the
reminder is marked delivered and its failed channel names are retained. If every
channel fails, it is marked failed and is not retried in a tight loop; updating
or snoozing it returns it to pending.

## Architecture

`ReminderManager` owns the in-memory records, mutations, persistence snapshots,
and the single next-due callback. Delivery is delegated to providers through
logical `DeliveryPolicy` values. Home Assistant actions and future conversation
tools call the manager rather than accessing storage or physical endpoints.

Storage writes occur only on persistent changes. Normal CRUD bursts use
`Store.async_delay_save`; delivery claim/result transitions use immediate saves
for sensible crash recovery. No reminder entities, per-reminder automations,
timers, minute polling, Recorder tables, or external databases are created.

## Current limitations and roadmap

- Voice delivery requires satellites that implement `assist_satellite.announce`;
  endpoint capabilities vary, so persistent/phone delivery remains the baseline.
- V1 does not include recurrence, acknowledgement buttons, retries/escalation,
  history retention controls, presence-aware satellite choice, a dedicated
  reminder dashboard, or direct conversation-agent tools.
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
