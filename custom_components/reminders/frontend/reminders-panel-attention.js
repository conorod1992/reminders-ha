import "./reminders-panel-native.js";

const Panel = customElements.get("reminders-management-panel");
const proto = Panel?.prototype;

if (proto && !proto.__needsAttentionInstalled) {
  proto.__needsAttentionInstalled = true;

  // The final panel owns the navigation tabs. This layer only enriches
  // attention cards and empty states, avoiding a redundant filter wrapper.
  const originalReminderCard = proto._reminderCard;

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
  if (reason === "automation_unavailable") return "Home Assistant rule problem — see the details below.";
  if (reason === "delivery_failed") return "Delivery failed — check this reminder or its delivery settings.";
  if (reason === "recent_delivery_failed") return "The most recent occurrence failed to deliver.";
  if (reason === "awaiting_acknowledgement") return "Waiting for you to dismiss this reminder.";
  if (reason === "action_available") return "This reminder still has an action for you to complete.";
  return "This reminder needs your attention.";
}

