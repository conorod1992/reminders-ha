# Reminders for Home Assistant

Reminders adds a dedicated reminder system to Home Assistant.

Create simple one-off reminders in a few clicks, or build recurring and context-aware reminders that can notify you in Home Assistant, on your phone, or through Assist speakers.

Reminders survive Home Assistant restarts, support multiple Home Assistant users, and include searchable history, quiet hours, snoozing, dismissal, optional task completion, triggered reminders, and automation/LLM support.

For most users, getting started is simply:

**Title → When → Save**

## Highlights

- Create one-time reminders with a simple **Title → When → Save** flow
- Quick choices for **10 minutes**, **30 minutes**, **1 hour**, **later today**, and **tomorrow morning**
- Daily, weekly, monthly, and yearly repeating reminders
- Reminders through:
  - Home Assistant persistent notifications
  - phone notifications
  - Assist satellites
- Optional **Snooze**, **Dismiss**, and **Done**
- Quiet hours for voice reminders
- Triggered reminders that wait for something to happen instead of a date/time
- Context-aware reminders such as:
  - “Remind me after 18:00 when I get home”
  - “Remind me at 21:00 unless Home Assistant detects that I already did it”
- Optional repeated reminders if something still needs attention
- Searchable reminder history
- Multiple Home Assistant users with separate reminders and preferences
- Friendly Home Assistant entity/user names in the panel
- Everything needed for normal use is available from the Reminders panel — YAML is optional
- Automation actions and optional conversation/LLM tools for advanced use
- Stable integration-facing upsert/reconciliation APIs for reminders owned by other integrations

Reminders does not create a Home Assistant entity or automation for every reminder, and it does not rely on polling.

## Requirements

- Home Assistant **2026.7 or newer**
- HACS is recommended for installation

## Installation

### Install with HACS

Because Reminders is installed as a custom HACS repository, you first need to add this repository to HACS.

1. Open **HACS** in Home Assistant.
2. Open the menu in the top-right corner and choose **Custom repositories**.
3. Paste:

   ```text
   https://github.com/conorod1992/reminders-ha
   ```

4. Choose **Integration** as the repository type/category.
5. Select **Add**.
6. Find **Reminders** in HACS.
7. Select **Download**.
8. Restart Home Assistant when prompted.

### Add Reminders to Home Assistant

Installing through HACS puts the integration files on your Home Assistant system. You still need to add the integration itself.

1. Go to **Settings → Devices & services**.
2. Select **Add integration**.
3. Search for **Reminders**.
4. Complete the one-step setup.
5. Open **Reminders** from the Home Assistant sidebar.

### Manual installation

If you prefer not to use HACS:

1. Copy `custom_components/reminders` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**.
4. Search for **Reminders** and complete setup.

## Quick start

To create your first reminder:

1. Open **Reminders** from the Home Assistant sidebar.
2. Select **Add reminder**.
3. Enter a title, for example **Put the bins out**.
4. Choose a quick time or select a date and time.
5. Select **Save**.

That is enough for a normal one-time reminder.

More options such as recurrence, custom delivery, automatic completion, repeated reminders, and recipient selection are available under **Advanced options**.

## First-run setup and delivery preferences

The first time you open Reminders, a setup dialog helps configure how reminders should reach you.

You can configure:

- Home Assistant persistent notifications
- phone notification targets
- Assist satellite voice targets
- whether reminders should normally stay active until dismissed

Persistent notifications are the simple, reliable default.

Phone and voice delivery are optional and can be skipped.

Entity choices use friendly Home Assistant names, so you do not need to type entity IDs into the panel.

You can dismiss the setup dialog and continue using Reminders normally.

Administrators always see the first-run setup for their own account. Managing another user's preferences is done separately from **Preferences**.

### Test your delivery settings

Preferences includes separate controls for:

- **Test notification**
- **Test phone**
- **Test voice**
- **Test configured delivery**

Tests use the targets currently selected in the form, even before you press Save.

A test sends:

> This is a test reminder from Home Assistant.

It does not create a reminder or history entry.

## Creating and managing reminders

The normal Add reminder form keeps the common options simple:

- title
- quick time choices
- exact date
- exact time

More advanced settings are placed under **Advanced options**.

These include:

- message/notes
- recurrence
- recipient
- custom delivery methods
- dismissal requirements
- optional manual completion
- quiet-hours override
- context-aware delivery
- automatic completion
- repeated reminders/escalation

Quick time choices simply fill in the normal date/time fields. You can still edit either before saving.

### Reminder actions

Depending on the reminder's settings, a reminder can offer:

- **Edit**
- **Duplicate**
- **Snooze**
- **Done**
- **Dismiss**
- **Delete**

Recurring reminders can additionally be paused, resumed, or have their next occurrence skipped.

**Done** appears only when manual completion is enabled.

**Dismiss** appears when that delivered reminder needs acknowledgement.

## Dismiss and Done

Dismiss and Done have different meanings.

- **Dismiss** means: “Stop reminding me about this occurrence.”
- **Done** means: “I completed the task.”

Not every reminder needs dismissal tracking.

Each user can choose whether reminders should normally stay active until dismissed. Individual reminders can override that default.

For recurring reminders, dismissing or completing one occurrence does not stop the series. The next scheduled occurrence continues normally.

### Technical YAML values

When using YAML/actions, dismissal behaviour is represented by:

- `default` — use the reminder owner's current preference
- `required` — this occurrence must be dismissed
- `not_required` — this reminder never requires dismissal

Manual task completion is enabled with:

```yaml
allow_manual_completion: true
```

The internal storage terms `awaiting_acknowledgement` and `acknowledged` are retained for compatibility, but the UI presents these as **awaiting dismissal** and **dismissed**.

## Duplicate reminders

Duplicating a reminder opens a new unsaved form.

For a one-time reminder:

- the title and other editable settings are copied
- the due time is deliberately left blank so you choose a new time

For a recurring reminder:

- the recurrence rule is copied
- the duplicate becomes a new independent series when saved

History, previous delivery state, acknowledgement state, and internal IDs are never copied.

## Search and views

Search looks through reminder titles and messages.

The panel has four primary views:

- **Needs attention** — reminders that currently require action or have a problem
- **Upcoming** — reminders waiting for their next scheduled delivery
- **All reminders** — the complete current reminder list
- **History** — resolved/delivered occurrence history

Under **All reminders**, the **Show** filter can narrow the list to:

- all reminder types
- recurring reminders
- triggered reminders
- failed reminders
- expired reminders

History is loaded in pages, with **Load more** available when additional rows exist.

These views update automatically as reminders change.

Administrators can explicitly filter by reminder owner using friendly Home Assistant display names.

## Reminder history

Each delivered or attempted reminder occurrence can record:

- original scheduled time
- current due time
- whether it was snoozed
- when it was delivered
- which delivery methods succeeded
- which delivery methods failed
- which channels were suppressed by quiet hours
- safe provider error types
- whether dismissal was required
- dismissal/completion time
- final outcome

History is stored by the integration itself. It does not create Recorder rows or one Home Assistant entity per occurrence.

### History retention

Default retention is:

- **90 days**
- **250 occurrences per reminder**

Both can be changed in Preferences.

Currently active occurrences and occurrences still waiting for dismissal are protected from pruning.

Deleting a reminder also deletes its stored history.

## Delivery methods

A reminder can use your current delivery defaults or its own custom delivery settings.

Supported methods are:

### Home Assistant notification

Uses Home Assistant persistent notifications.

Technical channel name:

```text
persistent_notification
```

### Phone

Uses Home Assistant Notify entities and/or explicitly selected Companion App notification services.

Technical channel name:

```text
phone
```

### Voice

Uses Assist satellites through `assist_satellite.announce`.

Technical channel name:

```text
voice
```

## Quiet hours

Quiet hours are configured separately for each Home Assistant user.

The default quiet-hours window is:

**23:00–07:00**

It is disabled until you choose to enable it.

By default, quiet hours:

- suppress voice announcements
- allow phone notifications to continue
- allow persistent notifications to continue
- add a persistent notification as a fallback when a selected voice reminder is suppressed

Quiet hours do **not** move the reminder to a later time.

The reminder is still considered due at its scheduled time. Its history records which delivery methods were suppressed.

Urgent reminders can optionally bypass quiet hours.

## Repeating reminders

Repeating reminders stay tied to the date and time you originally choose.

Snoozing a reminder or receiving it late does not gradually shift future occurrences.

Supported patterns include:

- every day
- every N days
- selected weekdays
- every N weeks
- a particular calendar day each month
- first through fifth weekday of a month
- last chosen weekday of a month
- last calendar day of a month
- yearly reminders
- optional end date
- optional maximum number of occurrences

The panel includes:

- Weekdays preset
- Weekends preset
- plain-English recurrence summaries
- preview of upcoming occurrences

### Examples

You can create patterns such as:

- every weekday at 08:00
- every second Tuesday
- the first Monday of every month
- the last Friday of every month
- the last day of every month
- every year on 1 December

### Month-end behaviour

A reminder set for the 31st stays a reminder for the 31st.

Months without a 31st are skipped rather than silently moving the reminder to the 30th or last day of the month.

Use the explicit **last day of the month** option if that is the behaviour you want.

A yearly reminder for 29 February similarly runs only in leap years.

### Missed occurrences

If Home Assistant is offline and multiple repeating occurrences pass, Reminders does not create a large backlog.

At most one representative overdue occurrence is delivered, then the recurrence advances directly to the next future scheduled occurrence.

## Smarter reminders

Advanced options allow Home Assistant state or events to affect when a reminder is delivered or when it should be considered completed.

Three useful concepts are:

### Deliver when

Wait until a condition is suitable before sending the reminder.

Example:

> Remind me to take the parcel to the car after 18:00, but only when I am home.

The due time becomes the earliest point at which the reminder can be delivered.

If the condition is not yet suitable, the reminder waits rather than becoming overdue.

### Complete when

Automatically resolve a reminder if Home Assistant detects that you already did the task.

Example:

> Remind me to brush my teeth at 22:30 unless brushing has already been detected.

Before delivery, automatic completion cancels that occurrence.

After delivery, it can resolve an occurrence that was still waiting for acknowledgement.

### Repeated reminders / escalation

A reminder can optionally notify you again while it is still waiting for acknowledgement.

For example:

> Remind me about the back door, then remind me again every hour up to three times if I have not dismissed it.

Repeated reminders are opt-in.

### YAML examples

You do not need YAML for normal panel use. These examples are for automations, scripts, or advanced configuration.

#### Deliver when

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

#### Complete when

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

#### Repeated reminders

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

`max_attempts` counts only the repeated reminders, not the original delivery.

## Triggered reminders

Triggered reminders wait for something to happen instead of a date/time.

Choose **When something happens** in the Add reminder form, select a trigger type, and fill in the fields shown.

Examples include:

- remind me when I arrive at a location
- remind me when a sensor changes state
- remind me when a numeric value drops below a threshold
- remind me when a Home Assistant event fires

Triggered reminders use the same:

- notification methods
- quiet hours
- snooze behaviour
- dismissal/completion options
- ownership rules
- history

### Supported trigger types

| Trigger type | What it means |
| --- | --- |
| State | An entity changes to/from a value |
| Numeric state | A number goes above or below a threshold |
| Zone | A person/entity enters or leaves a Home Assistant zone |
| Event | A Home Assistant event fires |
| Named trigger | Another automation/integration tells Reminders something happened |

### Trigger examples

#### State

```yaml
type: state
entity_id: sensor.work_status
to: Finished for today
```

#### Numeric state

```yaml
type: numeric_state
entity_id: sensor.printer_toner
below: 10
```

#### Zone

```yaml
type: zone
entity_id: person.conor
zone_entity_id: zone.woodies_carlow
event: enter
```

#### Event

```yaml
type: event
event_type: jarvis_opportunity
event_data:
  type: printing_started
```

#### Named trigger

```yaml
type: named
trigger_id: quiet_time_after_work
```

State triggers fire on an actual state change.

Numeric triggers fire when the value crosses into the requested range rather than on every update while it remains there.

Zone triggers use Home Assistant's normal zone/location handling.

`unknown`, `unavailable`, missing, and non-numeric values are treated as non-matches.

### Already-matching behaviour

By default, creating or restoring a reminder while its state/numeric/zone condition is already true does **not** immediately fire the reminder.

This prevents a Home Assistant restart from accidentally becoming a trigger.

Advanced options can enable already-matching behaviour when that is what you want.

Technical field:

```yaml
fire_if_already_matching: true
```

### Repeat policies

Triggered reminders support:

- `once`
- `every_trigger`
- `rearm_after_acknowledgement`

`once` is the default.

For `every_trigger`, `while_awaiting_acknowledgement` controls whether a new trigger is skipped while an older occurrence still needs dismissal or whether a new occurrence may be delivered.

Cooldown, start availability, expiry, duration matching, and already-matching behaviour are available under Advanced options.

## Triggered reminder actions

### Zone example

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

### State example

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

### Named trigger example

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

To fire a named trigger:

```yaml
action: reminders.fire_trigger
data:
  trigger_id: printing_started
```

## Named triggers

Named triggers allow another automation, integration, voice assistant, or decision system to tell Reminders that something has happened.

They are useful when the logic is more complex than Reminders' built-in state, numeric-state, zone, or event triggers.

For example, a normal Home Assistant automation can bridge an unsupported condition into Reminders:

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

That automation is only needed for the named trigger.

State, numeric-state, zone, and event reminders are listened for directly by Reminders.

Named-trigger IDs are normalized to lowercase and may contain:

- letters
- numbers
- underscores
- dots
- hyphens

## Companion App notification actions

Explicitly selected Home Assistant Companion App `notify.mobile_app_*` services can include reminder buttons.

Depending on the reminder, these may include:

- **Done**
- **Dismiss**
- snooze actions

Generic Notify entities receive the standard notification title/message only.

If a selected Companion App service rejects the action payload, Reminders retries that same service once as a normal notification without buttons.

No Home Assistant automation is required to handle these actions.

## Multiple Home Assistant users

Each Home Assistant user has their own:

- reminders
- reminder history
- delivery preferences
- quiet hours

Normal users can access only their own data.

Administrators can explicitly manage another user's reminders or preferences when needed.

Friendly names are used in the panel, but ownership is enforced using Home Assistant's internal user IDs in the backend.

## Home Assistant actions

You do **not** need actions or YAML to use Reminders.

Everything needed for ordinary reminder creation and management is available from the Reminders panel.

Actions are useful when automations, scripts, or other integrations need to create or manage reminders.

All public actions include Home Assistant action metadata so they are discoverable from the action editor.

### Reminder CRUD and lifecycle

- `reminders.create`
- `reminders.create_recurring`
- `reminders.create_triggered`
- `reminders.get`
- `reminders.list`
- `reminders.update`
- `reminders.delete`
- `reminders.snooze`
- `reminders.acknowledge`
- `reminders.complete`

### Recurring-series controls

- `reminders.pause`
- `reminders.resume`
- `reminders.skip_next`

### Preferences, delivery, and triggers

- `reminders.set_user_preferences`
- `reminders.test_delivery`
- `reminders.fire_trigger`

### Integration-facing actions

- `reminders.set_native_rules`
- `reminders.upsert`
- `reminders.reconcile_source`
- `reminders.external_action`

An authenticated action normally operates on the user who called it.

A system action without a user context must explicitly provide a valid `user_id`.

### Create a one-time reminder

```yaml
action: reminders.create
data:
  title: Put the bins out
  due: "2026-08-04 20:00:00"
  acknowledgement_policy: default
```

### Create a recurring reminder

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

Date/time values without an explicit timezone use Home Assistant's configured timezone.

Use `response_variable` if you want to capture the returned reminder data.

### Acknowledge a reminder

```yaml
action: reminders.acknowledge
data:
  reminder_id: 05d7c355-f394-40d6-b052-d5da1fc979cb
```

For recurring reminders with more than one outstanding historical occurrence, also provide `occurrence_id`.

### Test delivery from an action

```yaml
action: reminders.test_delivery
data:
  channels:
    - phone
  notify_targets:
    - notify.conors_phone
response_variable: delivery_test
```

The response includes successful and failed delivery methods plus safe error summaries.

## Using Reminders with conversation agents

Reminders can optionally expose reminder tools to compatible Home Assistant conversation agents.

This can allow requests such as:

- “Remind me to put the bins out at 8.”
- “What reminders do I have tomorrow?”
- “Snooze the bins reminder for an hour.”
- “Cancel my dentist reminder.”
- “Mark that reminder as done.”

To use this, enable the **Reminders** LLM API in the conversation agent's configuration.

Available tools include:

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

These tools use structured arguments and cannot execute arbitrary Home Assistant services.

Normal Home Assistant ownership rules still apply.

An ordinary user cannot read or change another user's reminders.

If a title matches zero reminders or more than one reminder, the tool does not guess. It returns enough safe information for the conversation agent to ask a follow-up question.

## Integration developer support

Other integrations can create and synchronize reminders without depending on Reminders' internal storage.

Supported source metadata fields include:

- `source`
- `source_id`
- `source_event`
- `managed_externally`

`create`, `create_recurring`, `create_triggered`, and `update` support these fields. `list` can filter by `source` and `source_id`.

For stable synchronization, prefer the dedicated integration-facing actions below.

### Stable external identity and upsert

`reminders.upsert` identifies an external reminder by:

**owner/user + `source` + `source_id`**

This allows the same source object to have separate reminders for different Home Assistant users without cross-user collisions.

The action accepts a reminder `kind` (`one_time`, `recurring`, or `triggered`) plus the complete desired create payload in `data`. It creates the reminder if the key does not exist or updates the existing externally managed reminder if it does.

An existing reminder under the key is never silently taken over if it is not marked `managed_externally`. Changing an existing external reminder to a different semantic kind also requires deleting/reconciling it first rather than silently converting it.

Example:

```yaml
action: reminders.upsert
data:
  source: annual_events
  source_id: passport_renewal
  kind: one_time
  data:
    title: Renew passport
    due: "2026-11-01 09:00:00"
```

### Reconcile an external source

`reminders.reconcile_source` lets an integration submit the complete set of source IDs that still exist for one owner/source pair.

Reminders atomically removes stale **externally managed** reminders for that scope. Ordinary user reminders are never swept. A reminder actively in the middle of delivery is skipped and reported in the response so the caller can safely reconcile again later.

### Home Assistant-native rules

`reminders.set_native_rules` exposes the same validated Home Assistant-native activation, delivery, delivery-condition, and completion rule lists used by the advanced panel editor.

The replacement is atomic: rule configuration is validated before the durable reminder state is changed.

### External actions

Externally managed reminders can provide up to five simple external action buttons.

Each action contains only:

- `id`
- `label`

Example:

```yaml
external_actions:
  - id: renewed
    label: Renewed
```

External actions cannot:

- call services
- run templates
- open URLs
- execute arbitrary callbacks

They can coexist with Snooze and Dismiss.

### Lifecycle events

Dismissal, manual completion, automatic completion, snooze, pause/resume, skip, external action selection, expiry, and deletion can fire the:

```text
reminders_lifecycle
```

Home Assistant event.

The current event schema is versioned with `schema_version: 1`. Its safe payload can include:

- `schema_version`
- `action`
- `reminder_id`
- `occurrence_id`
- `user_id`
- `source`
- `source_id`
- `source_event`
- `managed_externally`
- `activation_type`
- `recurring`
- `reminder_status`
- `occurrence_status`
- `event_time`
- `external_action_id` where relevant

Reminder titles and messages are deliberately excluded.

## Reliability and restart behaviour

Reminders persists its state across Home Assistant restarts.

Reminder creation, updates, deletion, snoozing, acknowledgement, recurrence advancement, rule changes, and delivery-state changes are written to storage before the new runtime state is treated as committed.

Before sending a reminder, due work is persisted as `delivering`.

If Home Assistant restarts during delivery, that claim is recovered and retried for both timed and triggered reminders.

This intentionally prefers not losing a reminder. In the rare case where an external notification service accepted a message immediately before Home Assistant crashed, a duplicate notification may be possible after restart.

A fully failed one-time reminder stops rather than entering an automatic retry loop.

For recurring reminders, a failed occurrence is recorded and the series advances to its next scheduled occurrence.

Repeated-reminder/escalation attempts are tracked separately and use their configured repeat delay.

## Recurrence timing and daylight saving time

Recurring reminders use the original local date/time as their schedule anchor.

This keeps repeating reminders stable even if an occurrence is:

- delivered late
- snoozed
- missed during downtime

For daylight saving time:

- an ambiguous autumn time uses the first occurrence
- a nonexistent spring time moves forward to the first valid local time after the gap

The same rules are used for both previews and actual delivery scheduling.

## Security and privacy

Reminder ownership is enforced in the backend using Home Assistant user IDs.

Every:

- action
- WebSocket command
- history query
- preference change
- delivery test
- conversation tool

checks the authenticated Home Assistant user.

The frontend panel is not treated as a security boundary.

Normal users can access only their own reminders, preferences, and history.

Administrators may explicitly target another user.

Diagnostics deliberately exclude:

- reminder text
- user IDs
- notification targets
- reminder history content

## Storage and migration

Storage schema 1.8 includes:

- anchored recurrence patterns and limits
- per-user delivery, dismissal, history, and quiet-hours preferences
- occurrence history and delivery/retry state
- triggered-reminder state, cooldowns, availability, and expiry
- context-aware delivery and automatic-completion state
- escalation state
- Companion App action state
- durable trigger-duration waits
- external source metadata and native rule configuration

Migration from storage versions 1.0–1.7 preserves compatible reminder data, including IDs, owners, content, due times, recurrence anchors, source metadata, and existing history.

Migration also supplies conservative defaults for features introduced after older schemas, including acknowledgement, quiet hours, trigger lifecycle state, escalation, mobile actions, and durable trigger-duration tracking.

Malformed individual records are isolated rather than causing the entire Reminders store to be discarded.

## Deliberate limitations

- Companion App notification actions require an explicitly selected `notify.mobile_app_*` service.
- Generic Notify entities receive normal notifications without Reminders action buttons.
- Conversation tools work only when the Reminders LLM API is enabled for the selected conversation agent.
- Conversation-agent user security depends on Home Assistant request context being passed correctly.
- Raw RRULE import/export is not the primary UI.
- Failed initial deliveries do not automatically retry forever.
- Repeated reminders/escalation must be enabled explicitly.
- Quiet hours use Home Assistant's configured timezone because Home Assistant does not currently expose a separate timezone for each user.
- Home Assistant 2026.7 does not expose a valid user selector for generic action schemas, so advanced admin `user_id` fields may appear as text fields in action configuration.
- The bounded built-in trigger editor deliberately does not include arbitrary service execution, webhooks, custom radius calculations, or automatic automation generation.

For more complex logic, use Home Assistant-native rules, a normal Home Assistant automation, or a named trigger.

### Multi-user permission boundaries

Reminders follows Home Assistant user permissions. Non-administrator reminder owners can only use classic state, numeric-state and zone triggers for entities they may read, and phone/Assist entity targets they may control. Home Assistant event triggers, arbitrary Home Assistant-native rules, and explicit Companion App service targets require an administrator-owned reminder because Home Assistant does not expose a safe per-user permission check for those global/service-level capabilities. Permissions are checked again when a delayed reminder actually delivers, so revoking access also revokes future delivery to that target.

## Technical architecture

For users interested in how Reminders avoids unnecessary Home Assistant overhead:

- no per-reminder Home Assistant entities
- no per-reminder automations
- no polling loop
- no Recorder tables
- no external database
- no per-occurrence Home Assistant objects
- one exact next-due callback is scheduled for the integration
- equivalent bounded trigger definitions can share listeners
- Home Assistant-native rule listeners are attached only while relevant
- trigger listeners are rebuilt from storage at startup
- unused listeners are removed after edits, deletion, completion, expiry, or unload
- history is kept in bounded integration storage

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

CI additionally runs Hassfest and HACS validation.

See [LICENSE](LICENSE).
