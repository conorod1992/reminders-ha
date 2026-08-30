import "./reminders-panel-attention.js";

const Panel = customElements.get("reminders-management-panel");
const proto = Panel?.prototype;

if (proto && !proto.__robustStartupInstalled) {
  proto.__robustStartupInstalled = true;

  const originalShowError = proto._showError;

  proto._showError = function (error) {
    originalShowError.call(this, error);

    // _start() intentionally handles its own error, so without resetting this
    // flag one transient startup failure leaves the connected panel unable to
    // initialise again until it is destroyed and recreated.
    if (this._started && !this._unsubscribe && !this._preferences) {
      this._started = false;
      const host = this.shadowRoot?.querySelector("#error");
      if (!host || host.querySelector(".startup-retry")) return;
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "secondary startup-retry";
      retry.textContent = "Retry";
      retry.style.marginInlineStart = "12px";
      retry.onclick = () => {
        retry.remove();
        if (!this._started) this._start();
      };
      host.append(retry);
    }
  };
}
