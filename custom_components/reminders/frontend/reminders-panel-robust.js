import "./reminders-panel-atomic.js";

const Panel = customElements.get("reminders-management-panel");
const proto = Panel?.prototype;
const RETRY_DELAYS_MS = [1000, 3000, 10000, 30000];

if (proto && !proto.__robustStartupInstalled) {
  proto.__robustStartupInstalled = true;

  const originalShowError = proto._showError;
  const originalClearError = proto._clearError;
  const originalDisconnected = proto.disconnectedCallback;

  proto._showError = function (error) {
    if (this._startupPhase) this._startupLoadError = error;
    originalShowError.call(this, error);
  };

  proto._clearError = function () {
    originalClearError.call(this);
    if (!this._startupPhase) _removeRetryUi(this);
  };

  proto._start = async function () {
    if (this._started || this._startupInFlight || !this._hass) return;

    _clearRetryTimer(this);
    this._started = true;
    this._startupInFlight = true;
    this._startupLoadError = null;
    try {
      if (this._hass.user?.is_admin) this._users = (await this._call("users")).users;
      this._preferences = (await this._call("get_preferences")).preferences;

      this._startupPhase = true;
      const loaded = await this._load();
      this._startupPhase = false;
      if (loaded === false || this._startupLoadError) {
        throw this._startupLoadError || new Error("Reminders could not be loaded.");
      }

      this._unsubscribe = await this._hass.connection.subscribeMessage(
        () => this._load(),
        { type: "reminders/subscribe" },
      );
      this._startupRetryAttempt = 0;
      _removeRetryUi(this);
      if (!this._preferences.configured) this._openPreferences(true);
    } catch (error) {
      this._startupPhase = false;
      if (this._unsubscribe) {
        this._unsubscribe();
        this._unsubscribe = undefined;
      }
      this._started = false;
      this._loading = false;
      this._renderList();
      originalShowError.call(this, error);
      _showRetryUi(this);
      _scheduleRetry(this);
    } finally {
      this._startupInFlight = false;
    }
  };

  proto.disconnectedCallback = function () {
    _clearRetryTimer(this);
    _removeRetryUi(this);
    this._startupInFlight = false;
    this._startupPhase = false;
    originalDisconnected.call(this);
  };
}

function _scheduleRetry(panel) {
  if (!panel.isConnected || panel._started || panel._startupRetryTimer) return;
  const attempt = panel._startupRetryAttempt || 0;
  const delay = RETRY_DELAYS_MS[Math.min(attempt, RETRY_DELAYS_MS.length - 1)];
  panel._startupRetryAttempt = attempt + 1;
  const status = panel.shadowRoot?.querySelector(".startup-retry-status");
  if (status) status.textContent = `Retrying automatically in ${Math.round(delay / 1000)}s.`;
  panel._startupRetryTimer = setTimeout(() => {
    panel._startupRetryTimer = undefined;
    if (panel.isConnected && !panel._started) panel._start();
  }, delay);
}

function _showRetryUi(panel) {
  const host = panel.shadowRoot?.querySelector("#error");
  if (!host || host.querySelector(".startup-retry")) return;

  const wrap = document.createElement("span");
  wrap.className = "startup-retry";
  wrap.style.display = "inline-flex";
  wrap.style.alignItems = "center";
  wrap.style.gap = "8px";
  wrap.style.marginInlineStart = "12px";

  const status = document.createElement("span");
  status.className = "startup-retry-status";
  status.textContent = "Retrying automatically.";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "secondary";
  retry.textContent = "Retry now";
  retry.onclick = () => {
    _clearRetryTimer(panel);
    _removeRetryUi(panel);
    if (!panel._started) panel._start();
  };
  wrap.append(status, retry);
  host.append(wrap);
}

function _removeRetryUi(panel) {
  panel.shadowRoot?.querySelector(".startup-retry")?.remove();
}

function _clearRetryTimer(panel) {
  if (!panel._startupRetryTimer) return;
  clearTimeout(panel._startupRetryTimer);
  panel._startupRetryTimer = undefined;
}
