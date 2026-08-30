import "./reminders-panel-robust.js";
import { canSnooze } from "./reminders-utils.js";

const Panel = customElements.get("reminders-management-panel");
const proto = Panel?.prototype;
const HISTORY_PAGE_SIZE = 50;
const LIST_PAGE_SIZE = 100;
const SECONDARY_VIEWS = new Set(["all", "recurring", "triggered", "failed", "expired"]);

if (proto && !proto.__frontendPolishInstalled) {
  proto.__frontendPolishInstalled = true;

  const originalRenderFilters = proto._renderFilters;
  const originalRenderList = proto._renderList;
  const originalReminderCard = proto._reminderCard;
  const originalHistoryCard = proto._historyCard;

  proto._load = async function (options = {}) {
    const historyView = this._view === "history";
    const appendHistory = historyView && options?.appendHistory === true;
    const appendList = !historyView && options?.appendList === true;

    if (appendHistory) {
      if (this._historyLoadingMore) return true;
      if (this._history.length >= (this._historyTotal || 0)) return true;
    } else if (appendList) {
      if (this._listLoadingMore) return true;
      if (this._items.length >= (this._listTotal || 0)) return true;
    }

    const requestId = (this._loadRequestId || 0) + 1;
    this._loadRequestId = requestId;
    const scope = this._scope;
    const query = this._query || undefined;
    const selectedUser = this._selectedUser;
    const view = this._view;
    const existingValues = historyView ? this._history : this._items;
    const offset = appendHistory || appendList ? existingValues.length : 0;
    const appendBase = appendHistory || appendList ? [...existingValues] : [];

    if (historyView) {
      this._listTotal = 0;
      this._listLoadingMore = false;
      if (!appendHistory) this._historyLoadingMore = false;
    } else {
      this._historyTotal = 0;
      this._historyLoadingMore = false;
      if (!appendList) this._listLoadingMore = false;
    }

    if (appendHistory) {
      this._historyLoadingMore = true;
    } else if (appendList) {
      this._listLoadingMore = true;
    } else {
      this._loading = true;
    }
    this._renderList();

    try {
      const data = { scope, query };
      if (scope === "user") data.user_id = selectedUser;
      if (historyView) {
        data.limit = HISTORY_PAGE_SIZE;
        data.offset = offset;
        const result = await this._call("history", data);
        if (requestId !== this._loadRequestId) return true;
        this._history = appendHistory
          ? [...appendBase, ...(result.history || [])]
          : (result.history || []);
        this._historyTotal = Number.isFinite(result.total)
          ? result.total
          : this._history.length;
      } else {
        data.view = view;
        data.limit = LIST_PAGE_SIZE;
        data.offset = offset;
        const result = await this._call("list", data);
        if (requestId !== this._loadRequestId) return true;
        this._items = appendList
          ? [...appendBase, ...(result.reminders || [])]
          : (result.reminders || []);
        this._listTotal = Number.isFinite(result.total)
          ? result.total
          : this._items.length;
      }
      this._clearError();
      return true;
    } catch (error) {
      if (requestId !== this._loadRequestId) return true;
      this._showError(error);
      return false;
    } finally {
      if (requestId === this._loadRequestId) {
        this._historyLoadingMore = false;
        this._listLoadingMore = false;
        this._loading = false;
        this._renderFilters();
        this._renderList();
      }
    }
  };

  proto._renderFilters = function () {
    originalRenderFilters.call(this);
    const root = this.shadowRoot;
    const tabs = root?.querySelector(".tabs");
    const toolbar = root?.querySelector(".toolbar");
    if (!tabs || !toolbar) return;

    tabs.classList.add("primary-tabs");
    tabs.replaceChildren();
    const primary = [
      ["attention", "Needs attention"],
      ["upcoming", "Upcoming"],
      ["all", "All reminders"],
      ["history", "History"],
    ];
    for (const [view, label] of primary) {
      const button = document.createElement("button");
      const active = view === "all" ? SECONDARY_VIEWS.has(this._view) : this._view === view;
      button.className = `tab${active ? " active" : ""}`;
      button.dataset.view = view;
      button.textContent = label;
      button.onclick = () => {
        this._view = view;
        this._load();
      };
      tabs.append(button);
    }

    toolbar.querySelector(".view-filter-wrap")?.remove();
    if (SECONDARY_VIEWS.has(this._view)) {
      const wrap = document.createElement("label");
      wrap.className = "view-filter-wrap";
      wrap.textContent = "Show";
      const select = document.createElement("select");
      select.className = "view-filter";
      for (const [value, label] of [
        ["all", "All reminder types"],
        ["recurring", "Recurring"],
        ["triggered", "Triggered"],
        ["failed", "Failed"],
        ["expired", "Expired"],
      ]) {
        select.add(new Option(label, value, false, value === this._view));
      }
      select.onchange = () => {
        this._view = select.value;
        this._load();
      };
      wrap.append(select);
      toolbar.prepend(wrap);
    }

    _installPolishStyles(root);
  };

  proto._renderList = function () {
    originalRenderList.call(this);
    if (this._loading) return;
    const host = this.shadowRoot?.querySelector("#list");
    const historyView = this._view === "history";
    const values = historyView ? this._history : this._items;
    if (!host || !values.length) return;

    const storedTotal = historyView ? this._historyTotal : this._listTotal;
    const total = Math.max(storedTotal || 0, values.length);
    const pageSize = historyView ? HISTORY_PAGE_SIZE : LIST_PAGE_SIZE;
    if (total <= pageSize && values.length >= total) return;

    const footer = document.createElement("div");
    footer.className = "pagination-footer";
    const count = document.createElement("span");
    count.className = "pagination-count";
    count.textContent = `Showing ${values.length} of ${total}`;
    footer.append(count);

    if (values.length < total) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary load-more";
      const loadingMore = historyView
        ? this._historyLoadingMore
        : this._listLoadingMore;
      button.disabled = Boolean(loadingMore);
      button.textContent = loadingMore ? "Loading…" : "Load more";
      button.onclick = () => this._load(
        historyView ? { appendHistory: true } : { appendList: true },
      );
      footer.append(button);
    }
    host.append(footer);
  };

  proto._reminderCard = function (reminder) {
    const card = originalReminderCard.call(this, reminder);
    _polishReminderCard(card, reminder);
    return card;
  };

  proto._historyCard = function (row) {
    const card = originalHistoryCard.call(this, row);
    _polishMeta(card);
    return card;
  };
}

function _polishReminderCard(card, reminder) {
  card.classList.add("polished-card");
  if (!canSnooze(reminder)) {
    for (const button of card.querySelectorAll(".actions button")) {
      if (button.textContent.trim() === "Snooze") button.remove();
    }
  }
  const body = card.children[1];
  const title = body?.querySelector(".name");
  if (title && !body.querySelector(".title-row")) {
    const row = document.createElement("div");
    row.className = "title-row";
    title.replaceWith(row);
    row.append(title);

    const statusValue = reminder.paused ? "paused" : reminder.status;
    if (statusValue && statusValue !== "pending") {
      const status = document.createElement("span");
      status.className = `status ${statusValue}`;
      status.textContent = _statusText(statusValue);
      row.append(status);
    }
  }
  _polishMeta(card);
}

function _polishMeta(card) {
  const meta = card.querySelector(".meta");
  if (!meta) return;
  const parts = meta.textContent
    .split(" · ")
    .map((value) => value.trim())
    .filter(Boolean);
  if (!parts.length) {
    meta.remove();
    return;
  }

  const list = document.createElement("div");
  list.className = "card-meta-list";
  for (const text of parts) {
    const item = document.createElement("span");
    item.className = "card-meta-item";
    item.textContent = text;
    list.append(item);
  }
  if (parts.length >= 4) card.classList.add("complex-card");
  meta.replaceWith(list);
}

function _statusText(value) {
  if (value === "acknowledged") return "Dismissed";
  if (value === "awaiting_acknowledgement") return "Awaiting dismissal";
  return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}

function _installPolishStyles(root) {
  if (!root || root.querySelector("#reminders-polish-styles")) return;
  const style = document.createElement("style");
  style.id = "reminders-polish-styles";
  style.textContent = `
    .primary-tabs{gap:4px}
    .primary-tabs .tab{white-space:nowrap;padding-inline:14px}
    .toolbar{align-items:end}
    .view-filter-wrap{display:grid;gap:4px;flex:0 0 auto;font-size:12px;color:var(--secondary-text-color)}
    .view-filter{min-width:155px;padding-block:9px}
    .card{align-items:start}
    .card .when{padding-top:2px}
    .card .actions{align-self:start}
    .title-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    .card-meta-list{display:flex;flex-wrap:wrap;gap:6px;margin-top:9px}
    .card-meta-item{display:inline-flex;align-items:center;min-height:26px;padding:4px 8px;border-radius:7px;background:var(--secondary-background-color);color:var(--secondary-text-color);font-size:12px;line-height:1.35}
    .complex-card{row-gap:12px}
    .complex-card .message{max-width:72ch}
    .pagination-footer{grid-column:1/-1;display:flex;align-items:center;justify-content:center;gap:12px;padding:18px 8px 6px;color:var(--secondary-text-color)}
    .pagination-footer .load-more{min-width:110px}
    .pagination-count{font-size:13px}
    @media(max-width:720px){
      .primary-tabs .tab{padding-inline:11px}
      .toolbar{align-items:stretch}
      .view-filter-wrap{width:100%}
      .view-filter{width:100%}
      .pagination-footer{flex-direction:column}
    }
  `;
  root.append(style);
}
