import "./reminders-panel-native.js";

const Panel = customElements.get("reminders-management-panel");
const proto = Panel?.prototype;

if (proto && !proto.__needsAttentionInstalled) {
  proto.__needsAttentionInstalled = true;

  const originalRenderFilters = proto._renderFilters;
  const originalRenderList = proto._renderList;
  const originalReminderCard = proto._reminderCard;

  proto._renderFilters = function () {
    originalRenderFilters.call(this);
    const tabs = this.shadowRoot?.querySelector(".tabs");
    if (!tabs || tabs.querySelector('[data-view="attention"]')) return;

    const button = document.createElement("button");
    button.dataset.view = "attention";
    button.className = `tab${this._view === "attention" ? " active" : ""}`;
    button.textContent = "Needs attention";
    button.onclick = () => {
      this._view = "attention";
      this._load();
    };
    tabs.prepend(button);
    _installAttentionStyles(this.shadowRoot);
  };

  proto._renderList = function () {
    originalRenderList.call(this);
    if (this._view !== "attention" || this._loading || this._items.length) return;
    const text = this.shadowRoot?.querySelector("#list .empty p");
    if (text && !this._query) text.textContent = "Nothing needs your attention";
  };

  proto._reminderCard = function (reminder) {
    const card = originalReminderCard.call(this, reminder);
    if (!reminder.attention_reason) return card;

    card.classList.add("attention-card");
    const body = card.children[1];
    const note = document.createElement("div");
    note.className = `attention-note ${reminder.attention_reason}`;
    note.textContent = _attentionText(reminder.attention_reason);
    body?.querySelector(".name")?.after(note);
    return card;
  };
}

function _attentionText(reason) {
  if (reason === "delivery_failed") return "Delivery failed — check this reminder or its delivery settings.";
  if (reason === "recent_delivery_failed") return "The most recent occurrence failed to deliver.";
  if (reason === "awaiting_acknowledgement") return "Waiting for you to dismiss this reminder.";
  if (reason === "action_available") return "This reminder still has an action for you to complete.";
  return "This reminder needs your attention.";
}

function _installAttentionStyles(root) {
  if (!root || root.querySelector("#reminders-attention-styles")) return;
  const style = document.createElement("style");
  style.id = "reminders-attention-styles";
  style.textContent = `
    .attention-card{border-inline-start:4px solid var(--warning-color)}
    .attention-note{margin-top:5px;font-size:13px;font-weight:500;color:var(--warning-color)}
    .attention-note.delivery_failed,.attention-note.recent_delivery_failed{color:var(--error-color)}
  `;
  root.append(style);
}
