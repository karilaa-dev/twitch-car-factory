(() => {
  "use strict";

  const livePanel = document.querySelector("[data-live-status]");
  if (!livePanel) return;

  const endpoint = livePanel.dataset.statusUrl;
  const intervalMs = 5000;
  let timer;
  let inFlight = false;

  const setIndicator = (state, label) => {
    const indicator = document.querySelector("[data-poll-indicator]");
    if (!indicator) return;
    indicator.dataset.state = state;
    indicator.textContent = label;
  };

  const schedule = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(refresh, intervalMs);
  };

  const refresh = async () => {
    const current = document.querySelector("[data-live-status]");
    if (!current || inFlight || document.hidden) {
      schedule();
      return;
    }

    if (current.contains(document.activeElement)) {
      setIndicator("paused", "Update held while controls are focused");
      schedule();
      return;
    }

    inFlight = true;
    try {
      const response = await fetch(endpoint, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }
      if (!response.ok) throw new Error(`Status request failed: ${response.status}`);
      const html = await response.text();
      current.outerHTML = html;
      setIndicator("ok", "Telemetry updated just now");
    } catch (_error) {
      setIndicator("error", "Telemetry link interrupted");
    } finally {
      inFlight = false;
      schedule();
    }
  };

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();
  });
  schedule();
})();

(() => {
  "use strict";

  const dialog = document.querySelector("[data-account-info-dialog]");
  if (!dialog) return;

  const content = dialog.querySelector("[data-account-info-content]");
  const closeButton = dialog.querySelector("[data-dialog-close]");
  const triggers = document.querySelectorAll("[data-account-info-trigger]");
  let returnFocus = null;
  let requestController = null;

  const loadingMarkup = `
    <div class="dialog-loading" role="status">
      <span class="status-light" data-tone="pending" aria-hidden="true"></span>
      Loading account information…
    </div>`;

  const errorMarkup = `
    <div class="message error" role="alert">
      Account information could not be loaded. Close this window and try again.
    </div>`;

  const openDialog = async (trigger) => {
    const endpoint = trigger.dataset.accountInfoUrl;
    if (!endpoint) return;

    requestController?.abort();
    requestController = new AbortController();
    returnFocus = trigger;
    trigger.setAttribute("aria-expanded", "true");
    content.innerHTML = loadingMarkup;

    if (!dialog.open) {
      dialog.showModal();
    }
    closeButton?.focus();

    try {
      const response = await fetch(endpoint, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { "X-Requested-With": "XMLHttpRequest" },
        signal: requestController.signal,
      });
      if (response.redirected) {
        window.location.assign(response.url);
        return;
      }
      if (!response.ok) throw new Error(`Account request failed: ${response.status}`);
      content.innerHTML = await response.text();
    } catch (error) {
      if (error.name !== "AbortError") content.innerHTML = errorMarkup;
    }
  };

  triggers.forEach((trigger) => {
    trigger.setAttribute("aria-expanded", "false");
    trigger.addEventListener("click", () => openDialog(trigger));
  });

  closeButton?.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    requestController?.abort();
    triggers.forEach((trigger) => trigger.setAttribute("aria-expanded", "false"));
    if (returnFocus?.isConnected) returnFocus.focus();
    returnFocus = null;
  });

  dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    const inside =
      event.clientX >= bounds.left &&
      event.clientX <= bounds.right &&
      event.clientY >= bounds.top &&
      event.clientY <= bounds.bottom;
    if (!inside) dialog.close();
  });
})();

(() => {
  "use strict";

  const form = document.querySelector("[data-import-upload-form]");
  if (!form) return;

  const fileInput = form.querySelector('input[type="file"]');
  const summary = form.querySelector("[data-import-file-summary]");
  const dropTarget = fileInput?.closest(".upload-drop");
  if (!fileInput || !summary || !dropTarget) return;

  const formatSize = (bytes) => {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
  };

  fileInput.addEventListener("change", () => {
    const [file] = fileInput.files;
    dropTarget.classList.toggle("has-file", Boolean(file));
    summary.textContent = file
      ? `${file.name} · ${formatSize(file.size)}`
      : "Expected: config.yaml, data/state.json, optional presets and cookies";
  });
})();

(() => {
  "use strict";

  const form = document.querySelector("[data-import-confirm-form]");
  if (!form) return;

  const acknowledgement = form.querySelector("[data-import-acknowledgement]");
  const confirmation = form.querySelector("[data-import-confirmation]");
  const submit = form.querySelector("[data-import-submit]");
  if (!acknowledgement || !confirmation || !submit) return;

  const update = () => {
    submit.disabled = !(acknowledgement.checked && confirmation.value.trim() === "REPLACE");
  };

  acknowledgement.addEventListener("change", update);
  confirmation.addEventListener("input", update);
  update();
})();
