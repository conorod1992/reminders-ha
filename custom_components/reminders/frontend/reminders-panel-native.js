import "./reminders-panel.js";

const Panel = customElements.get("reminders-management-panel");
const proto = Panel?.prototype;

if (proto && !proto.__nativeRuleEditorsInstalled) {
  proto.__nativeRuleEditorsInstalled = true;

  const originalOpen = proto._openReminderForm;
  const originalSave = proto._saveReminder;
  const originalTriggerData = proto._triggerData;
  const originalCall = proto._call;
  const originalSummary = proto._updateBehaviorSummary;

  proto._openReminderForm = function (reminder = null, duplicate = null) {
    originalOpen.call(this, reminder, duplicate);
    const source = reminder || duplicate;
    const form = this.shadowRoot?.querySelector("#reminder-form");
    if (!form) return;
    if (!this._hass?.user?.is_admin) return;

    if (_containsNamedLegacyRule(source)) {
      _addClassicNotice(form);
      return;
    }

    const activation = source?.activation_triggers?.length
      ? _clone(source.activation_triggers)
      : _legacyTriggerList(source?.trigger);
    const completion = source?.completion_triggers?.length
      ? _clone(source.completion_triggers)
      : _legacyTriggerList(source?.complete_when);
    const deliveryTriggers = source?.delivery_triggers?.length
      ? _clone(source.delivery_triggers)
      : _legacyDeliveryTriggers(source?.deliver_when);
    const deliveryConditions = source?.delivery_conditions?.length
      ? _clone(source.delivery_conditions)
      : _legacyDeliveryConditions(source?.deliver_when);

    form._nativeRules = {
      activation,
      completion,
      deliveryTriggers,
      deliveryConditions,
      editingNative: Boolean(source?.activation_triggers?.length),
      classic: false,
    };

    _installNativeStyles(form);
    _enhanceActivation(this, form);
    _enhanceCompletion(this, form);
    _enhanceDelivery(this, form);
    _installClassicFallback(this, form);
    _refreshNativeVisibility(form);
    this._updateBehaviorSummary(form);
  };

  proto._triggerData = function (form, prefix = "") {
    if (form?._nativeRules && !form._nativeRules.classic) {
      return { type: "named", trigger_id: `native_${prefix || "activation"}` };
    }
    return originalTriggerData.call(this, form, prefix);
  };

  proto._saveReminder = async function (dialog, form, reminder) {
    if (!form?._nativeRules || form._nativeRules.classic) {
      return originalSave.call(this, dialog, form, reminder);
    }
    const triggered = form.elements.activation_type.value === "trigger";
    const deliveryEnabled = form.elements.deliver_when_enabled.checked;
    const completionEnabled = form.elements.complete_when_enabled.checked;
    const rules = form._nativeRules;

    if (triggered && !rules.activation.length) {
      return _formError(form, "Add at least one trigger for this reminder.");
    }
    if (deliveryEnabled && !rules.deliveryTriggers.length && !rules.deliveryConditions.length) {
      return _formError(form, "Add a Wait for trigger, an Only if condition, or turn delivery rules off.");
    }
    if (completionEnabled && !rules.completion.length) {
      return _formError(form, "Add at least one automatic-completion trigger.");
    }

    this._nativeSaveState = {
      rules: {
        activation: triggered ? _clone(rules.activation) : [],
        completion: completionEnabled ? _clone(rules.completion) : [],
        deliveryTriggers: !triggered && deliveryEnabled ? _clone(rules.deliveryTriggers) : [],
        deliveryConditions: !triggered && deliveryEnabled ? _clone(rules.deliveryConditions) : [],
      },
      reminder,
      triggered,
    };
    try {
      return await originalSave.call(this, dialog, form, reminder);
    } finally {
      this._nativeSaveState = null;
    }
  };

  proto._call = async function (action, payload = {}) {
    const state = this._nativeSaveState;
    if (!state) return originalCall.call(this, action, payload);

    const rules = state.rules;
    if (action === "create_triggered") {
      const data = _without(payload, [
        "trigger",
        "deliver_when",
        "complete_when",
        "fire_if_already_matching",
        "expires_after_seconds",
        "missed_occurrence_policy",
      ]);
      data.activation_triggers = rules.activation;
      data.completion_triggers = rules.completion;
      return originalCall.call(this, "create_native_triggered", data);
    }

    if (action === "update" && state.triggered) {
      const data = _without(payload, [
        "activation_type",
        "trigger",
        "deliver_when",
        "complete_when",
        "fire_if_already_matching",
        "expires_after_seconds",
        "missed_occurrence_policy",
      ]);
      data.activation_triggers = rules.activation;
      data.completion_triggers = rules.completion;
      return originalCall.call(this, "update_native_triggered", data);
    }

    if (action === "create" || action === "create_recurring") {
      const data = _without(payload, ["deliver_when", "complete_when"]);
      const created = await originalCall.call(this, action, data);
      const reminderId = created?.reminder?.id;
      if (!reminderId) return created;
      try {
        const updated = await originalCall.call(this, "set_native_rules", {
          reminder_id: reminderId,
          delivery_triggers: rules.deliveryTriggers,
          delivery_conditions: rules.deliveryConditions,
          completion_triggers: rules.completion,
        });
        return updated || created;
      } catch (error) {
        try {
          await originalCall.call(this, "delete", { reminder_id: reminderId });
        } catch (rollbackError) {
          const detail = rollbackError?.message || String(rollbackError);
          error.message = `${error?.message || String(error)} The incomplete reminder could not be removed automatically: ${detail}`;
        }
        throw error;
      }
    }

    if (action === "update" && !state.triggered) {
      const currentWasNativeTriggered = Boolean(state.reminder?.activation_triggers?.length);
      if (currentWasNativeTriggered) {
        throw new Error(
          "A native trigger-based reminder cannot be changed directly into a scheduled reminder. Duplicate it as a scheduled reminder instead."
        );
      }
      const data = _without(payload, ["deliver_when", "complete_when"]);
      const updated = await originalCall.call(this, action, data);
      await originalCall.call(this, "set_native_rules", {
        reminder_id: payload.reminder_id,
        delivery_triggers: rules.deliveryTriggers,
        delivery_conditions: rules.deliveryConditions,
        completion_triggers: rules.completion,
      });
      return updated;
    }

    return originalCall.call(this, action, payload);
  };

  proto._updateBehaviorSummary = function (form) {
    originalSummary.call(this, form);
    if (!form?._nativeRules || form._nativeRules.classic) return;
    const list = form.querySelector(".behavior-summary ul");
    if (!list) return;
    const triggered = form.elements.activation_type.value === "trigger";
    const items = [...list.querySelectorAll("li")];
    if (triggered && items.length) {
      items[0].textContent = form._nativeRules.activation.length === 1
        ? "activate when the selected Home Assistant trigger fires"
        : `activate when any of ${form._nativeRules.activation.length || "the selected"} Home Assistant triggers fire`;
    }
    if (!triggered && form.elements.deliver_when_enabled.checked) {
      const old = items.find((item) => item.textContent.includes("wait until the selected delivery condition"));
      if (old) {
        const wake = form._nativeRules.deliveryTriggers.length;
        const conditions = form._nativeRules.deliveryConditions.length;
        old.textContent = wake && conditions
          ? "wait for the selected Home Assistant triggers, then deliver only if the selected conditions are true"
          : wake
            ? "wait for the selected Home Assistant trigger after the scheduled time"
            : "deliver at the scheduled time only if the selected Home Assistant conditions are true";
      }
    }
    if (form.elements.complete_when_enabled.checked) {
      const old = items.find((item) => item.textContent.includes("mark itself Done"));
      if (old) old.textContent = "mark itself Done when the selected Home Assistant completion trigger fires";
    }
  };
}

function _enhanceActivation(panel, form) {
  const host = form.querySelector("#trigger-activation");
  if (!host) return;
  const legacy = document.createElement("div");
  legacy.className = "native-legacy-fields";
  while (host.firstChild) legacy.append(host.firstChild);
  _disableLegacyFields(legacy);
  const section = _section(
    "When something happens",
    "Choose one or more Home Assistant triggers. Any trigger can activate the reminder.",
  );
  const editorHost = section.querySelector(".native-editor-host");
  host.append(legacy, section);
  _mountTriggerEditor(panel, editorHost, form._nativeRules.activation, (value) => {
    form._nativeRules.activation = value;
    panel._updateBehaviorSummary(form);
  });
}

function _enhanceCompletion(panel, form) {
  const enabled = form.elements.complete_when_enabled;
  const legacy = form.querySelector(".complete_when-config");
  if (!enabled || !legacy) return;
  _disableLegacyFields(legacy);
  legacy.classList.add("native-legacy-fields");
  const section = _section(
    "Completion trigger",
    "Mark this reminder Done when any selected Home Assistant trigger fires.",
  );
  section.classList.add("native-completion-section");
  legacy.after(section);
  _mountTriggerEditor(panel, section.querySelector(".native-editor-host"), form._nativeRules.completion, (value) => {
    form._nativeRules.completion = value;
    panel._updateBehaviorSummary(form);
  });
  enabled.addEventListener("change", () => {
    _refreshNativeVisibility(form);
    panel._updateBehaviorSummary(form);
  });
}

function _enhanceDelivery(panel, form) {
  const enabled = form.elements.deliver_when_enabled;
  const legacy = form.querySelector(".deliver_when-config");
  if (!enabled || !legacy) return;
  _disableLegacyFields(legacy);
  legacy.classList.add("native-legacy-fields");

  const label = enabled.closest("label");
  const spans = label?.querySelectorAll("span");
  if (spans?.[0]) spans[0].lastChild.textContent = " Use delivery rules after the scheduled time";
  if (spans?.[1]) spans[1].textContent = "Use normal Home Assistant triggers and conditions to control when a due reminder may be delivered.";

  const wrapper = document.createElement("div");
  wrapper.className = "native-delivery-section";
  const triggers = _section(
    "Wait for (optional)",
    "After the reminder is due, these triggers cause its conditions to be checked. If there are no conditions, the first trigger delivers it.",
  );
  const conditions = _section(
    "Only if (optional)",
    "These are ordinary Home Assistant conditions. Multiple top-level conditions must all be true; use AND, OR and NOT blocks for more complex rules.",
  );
  wrapper.append(triggers, conditions);
  legacy.after(wrapper);
  _mountTriggerEditor(panel, triggers.querySelector(".native-editor-host"), form._nativeRules.deliveryTriggers, (value) => {
    form._nativeRules.deliveryTriggers = value;
    panel._updateBehaviorSummary(form);
  });
  _mountConditionEditor(panel, conditions.querySelector(".native-editor-host"), form._nativeRules.deliveryConditions, (value) => {
    form._nativeRules.deliveryConditions = value;
    panel._updateBehaviorSummary(form);
  });
  enabled.addEventListener("change", () => {
    _refreshNativeVisibility(form);
    panel._updateBehaviorSummary(form);
  });
}

function _mountTriggerEditor(panel, host, value, changed) {
  const editor = document.createElement("ha-selector");
  editor.hass = panel._hass;
  editor.selector = { trigger: {} };
  editor.value = _clone(value);
  editor.required = false;
  editor.addEventListener("value-changed", (event) => changed(_clone(event.detail?.value || [])));
  host.replaceChildren(editor);
}

function _mountConditionEditor(panel, host, value, changed) {
  const editor = document.createElement("ha-selector");
  editor.hass = panel._hass;
  editor.selector = { condition: {} };
  editor.value = _clone(value);
  editor.required = false;
  editor.addEventListener("value-changed", (event) => changed(_clone(event.detail?.value || [])));
  host.replaceChildren(editor);
}

function _section(title, hint) {
  const section = document.createElement("div");
  section.className = "native-rule-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  const help = document.createElement("div");
  help.className = "hint";
  help.textContent = hint;
  const host = document.createElement("div");
  host.className = "native-editor-host";
  section.append(heading, help, host);
  return section;
}

function _installClassicFallback(panel, form) {
  const details = document.createElement("details");
  details.className = "native-classic-fallback";
  details.innerHTML = `<summary>Need the Reminders named trigger?</summary><div class="hint">Named trigger IDs are specific to Reminders and are not part of Home Assistant's native trigger editor. Switch back to the classic editor for that advanced feature.</div><button type="button" class="secondary">Use classic trigger editor</button>`;
  const activation = form.querySelector("#trigger-activation");
  activation?.append(details);
  details.querySelector("button").onclick = () => {
    form._nativeRules.classic = true;
    for (const legacy of form.querySelectorAll(".native-legacy-fields")) {
      legacy.classList.remove("native-legacy-fields");
      for (const field of legacy.querySelectorAll("input,select,textarea")) field.disabled = false;
    }
    for (const native of form.querySelectorAll(".native-rule-section,.native-delivery-section,.native-classic-fallback")) native.classList.add("hidden");
    panel._syncActivationUI(form);
    panel._syncAdvancedContexts(form);
    panel._updateBehaviorSummary(form);
  };
}

function _addClassicNotice(form) {
  const activation = form.querySelector("#trigger-activation");
  if (!activation) return;
  const note = document.createElement("div");
  note.className = "expert-note";
  note.textContent = "This reminder uses a Reminders named trigger, which is not a Home Assistant-native trigger type. It remains in the classic editor so it can be edited without changing its behaviour.";
  activation.prepend(note);
}

function _refreshNativeVisibility(form) {
  form.querySelector(".native-completion-section")?.classList.toggle("hidden", !form.elements.complete_when_enabled.checked);
  form.querySelector(".native-delivery-section")?.classList.toggle("hidden", !form.elements.deliver_when_enabled.checked);
}

function _disableLegacyFields(host) {
  for (const field of host.querySelectorAll("input,select,textarea")) {
    field.disabled = true;
    field.required = false;
  }
}

function _installNativeStyles(form) {
  const style = document.createElement("style");
  style.textContent = `
    .native-legacy-fields{display:none!important}
    .native-rule-section{display:grid;gap:10px;margin-top:10px;padding:12px;border:1px solid var(--divider-color);border-radius:9px}
    .native-rule-section h3{margin:0;font-size:15px}
    .native-editor-host{min-width:0}
    .native-delivery-section{display:grid;gap:12px}
    .native-classic-fallback{margin-top:12px}
    .native-classic-fallback button{margin-top:10px}
  `;
  form.append(style);
}

function _containsNamedLegacyRule(source) {
  return [source?.trigger, source?.deliver_when, source?.complete_when].some((rule) => rule?.type === "named");
}

function _legacyTriggerList(rule) {
  if (!rule) return [];
  if (rule.type === "named") return [];
  const result = {};
  if (rule.type === "state") {
    result.trigger = "state";
    result.entity_id = rule.entity_id;
    if (rule.from !== undefined) result.from = rule.from;
    if (rule.to !== undefined) result.to = rule.to;
    if (rule.attribute) result.attribute = rule.attribute;
    if (rule.for_seconds) result.for = { seconds: rule.for_seconds };
  } else if (rule.type === "numeric_state") {
    result.trigger = "numeric_state";
    result.entity_id = rule.entity_id;
    if (rule.above !== undefined) result.above = rule.above;
    if (rule.below !== undefined) result.below = rule.below;
    if (rule.attribute) result.attribute = rule.attribute;
    if (rule.for_seconds) result.for = { seconds: rule.for_seconds };
  } else if (rule.type === "zone") {
    result.trigger = "zone";
    result.entity_id = rule.entity_id;
    result.zone = rule.zone_entity_id;
    result.event = rule.event || "enter";
  } else if (rule.type === "event") {
    result.trigger = "event";
    result.event_type = rule.event_type;
    if (rule.event_data) result.event_data = _clone(rule.event_data);
  } else return [];
  return [result];
}

function _legacyDeliveryTriggers(rule) {
  return _legacyTriggerList(rule);
}

function _legacyDeliveryConditions(rule) {
  if (!rule || rule.type === "event" || rule.type === "named") return [];
  if (rule.type === "state") {
    if (rule.to !== undefined) {
      const condition = { condition: "state", entity_id: rule.entity_id, state: rule.to };
      if (rule.attribute) condition.attribute = rule.attribute;
      if (rule.for_seconds) condition.for = { seconds: rule.for_seconds };
      return [condition];
    }
    return [];
  }
  if (rule.type === "numeric_state") {
    const condition = { condition: "numeric_state", entity_id: rule.entity_id };
    if (rule.above !== undefined) condition.above = rule.above;
    if (rule.below !== undefined) condition.below = rule.below;
    if (rule.attribute) condition.attribute = rule.attribute;
    return [condition];
  }
  if (rule.type === "zone") {
    const inside = { condition: "zone", entity_id: rule.entity_id, zone: rule.zone_entity_id };
    return rule.event === "leave" ? [{ condition: "not", conditions: [inside] }] : [inside];
  }
  return [];
}

function _without(value, keys) {
  const result = { ...value };
  for (const key of keys) delete result[key];
  return result;
}

function _clone(value) {
  return JSON.parse(JSON.stringify(value || []));
}

function _formError(form, message) {
  const host = form.querySelector(".form-error");
  host.textContent = message;
  host.classList.remove("hidden");
  host.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return undefined;
}
