import "./reminders-panel-attention.js";

const Panel = customElements.get("reminders-management-panel");
const proto = Panel?.prototype;

if (proto && !proto.__atomicNativeScheduledInstalled) {
  proto.__atomicNativeScheduledInstalled = true;

  // reminders-panel-native.js already intercepts create/update calls while its
  // editor is saving. Wrap that interception narrowly for existing time-based
  // reminders so the ordinary fields and native rules reach the backend in one
  // command instead of two independently durable writes.
  const nativeCall = proto._call;

  proto._call = async function (action, payload = {}) {
    const state = this._nativeSaveState;
    if (!state || action !== "update" || state.triggered) {
      return nativeCall.call(this, action, payload);
    }

    if (state.reminder?.activation_triggers?.length) {
      throw new Error(
        "A native trigger-based reminder cannot be changed directly into a scheduled reminder. Duplicate it as a scheduled reminder instead."
      );
    }

    const data = _without(payload, ["deliver_when", "complete_when"]);
    data.delivery_triggers = _clone(state.rules.deliveryTriggers);
    data.delivery_conditions = _clone(state.rules.deliveryConditions);
    data.completion_triggers = _clone(state.rules.completion);

    // Call the native wrapper with its save state temporarily hidden. This lets
    // it fall through to the panel's raw WebSocket transport instead of entering
    // its legacy update + set_native_rules sequence recursively.
    const savedState = this._nativeSaveState;
    this._nativeSaveState = null;
    try {
      return await nativeCall.call(this, "update_native_scheduled", data);
    } finally {
      this._nativeSaveState = savedState;
    }
  };
}

function _without(value, keys) {
  const result = { ...value };
  for (const key of keys) delete result[key];
  return result;
}

function _clone(value) {
  return JSON.parse(JSON.stringify(value || []));
}
