(() => {
  "use strict";

  const observer = "ResizeObserver" in window
    ? new ResizeObserver((entries) => entries.forEach(({ target }) => fit(target)))
    : null;

  const fit = (container) => {
    const items = [...container.querySelectorAll("[data-channel-item]")];
    const more = container.querySelector("[data-channel-more]");
    if (!more || !items.length) return;

    items.forEach((item) => { item.hidden = false; });
    more.hidden = true;
    if (container.scrollWidth <= container.clientWidth) return;

    for (let visible = items.length - 1; visible >= 0; visible -= 1) {
      items.forEach((item, index) => { item.hidden = index >= visible; });
      more.textContent = `and ${items.length - visible} more`;
      more.hidden = false;
      if (container.scrollWidth <= container.clientWidth) break;
    }
  };

  const initialize = (root = document) => {
    root.querySelectorAll("[data-channel-overflow]").forEach((container) => {
      fit(container);
      observer?.observe(container);
    });
  };

  window.controlRoomFitChannels = initialize;
  initialize();
  if (!observer) window.addEventListener("resize", () => initialize());
})();

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
      const documentFragment = new DOMParser().parseFromString(html, "text/html");
      const incoming = documentFragment.querySelector("[data-live-status]");
      if (!incoming) throw new Error("Status response was missing its live panel");

      const comparableMarkup = (panel) => {
        const clone = panel.cloneNode(true);
        const indicator = clone.querySelector("[data-poll-indicator]");
        if (indicator) {
          indicator.textContent = "";
          indicator.removeAttribute("data-state");
        }
        return clone.innerHTML;
      };
      if (comparableMarkup(current) !== comparableMarkup(incoming)) {
        current.replaceChildren(...incoming.childNodes);
        window.controlRoomFitChannels?.(current);
      }
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

  document.querySelectorAll("[data-channel-source-form]").forEach((form) => {
    const modeInputs = [...form.querySelectorAll('input[name="mode"]')];
    if (!modeInputs.length) return;

    const update = () => {
      const mode = modeInputs.find((input) => input.checked)?.value || "default";
      form.querySelectorAll("[data-source-field]").forEach((field) => {
        const visible = field.dataset.sourceField === mode;
        field.hidden = !visible;
        field.querySelectorAll("input, select, textarea").forEach((input) => {
          input.disabled = !visible;
        });
      });
    };

    modeInputs.forEach((input) => input.addEventListener("change", update));
    update();
  });
})();

(() => {
  "use strict";

  document.querySelectorAll("[data-preset-form]").forEach((form) => {
    const source = form.querySelector("[data-preset-channel-source]");
    const textarea = source?.querySelector("textarea");
    const editor = form.querySelector("[data-preset-channel-editor]");
    const input = editor?.querySelector("[data-preset-channel-input]");
    const addButton = editor?.querySelector("[data-preset-channel-add]");
    const list = editor?.querySelector("[data-preset-channel-list]");
    const feedback = editor?.querySelector("[data-preset-channel-feedback]");
    if (!source || !textarea || !editor || !input || !addButton || !list || !feedback) return;

    let channels = textarea.value
      .split(/[,\r\n]+/)
      .map((value) => value.trim())
      .filter(Boolean);

    const sync = () => { textarea.value = channels.join("\n"); };
    const announce = (message, error = false) => {
      feedback.textContent = message;
      feedback.dataset.tone = error ? "danger" : "neutral";
    };

    const render = () => {
      list.replaceChildren();
      channels.forEach((channel, index) => {
        const row = document.createElement("li");
        row.className = "preset-channel-row";

        const identity = document.createElement("span");
        identity.className = "preset-channel-row__identity";
        identity.innerHTML = `<span class="preset-channel-row__position">${String(index + 1).padStart(2, "0")}</span>`;
        const tag = document.createElement("span");
        tag.className = "channel-tag";
        tag.textContent = channel;
        identity.append(tag);

        const remove = document.createElement("button");
        remove.className = "button button--quiet button--danger";
        remove.type = "button";
        remove.textContent = "Delete";
        let confirmationTimer = null;
        remove.addEventListener("click", () => {
          if (remove.dataset.confirming === "true") {
            window.clearTimeout(confirmationTimer);
            channels.splice(index, 1);
            sync();
            render();
            announce(`${channel} removed from the staged preset.`);
            return;
          }
          remove.dataset.confirming = "true";
          remove.textContent = "Confirm";
          confirmationTimer = window.setTimeout(() => {
            if (!remove.isConnected) return;
            remove.dataset.confirming = "false";
            remove.textContent = "Delete";
          }, 5000);
        });

        row.append(identity, remove);
        list.append(row);
      });
      if (!channels.length) {
        const empty = document.createElement("li");
        empty.className = "preset-channel-list__empty";
        empty.textContent = "No channels staged. Add at least one before saving.";
        list.append(empty);
      }
    };

    const addChannel = () => {
      const channel = input.value.trim();
      if (!/^[A-Za-z0-9_]{1,100}$/.test(channel)) {
        announce("Use 1–100 letters, numbers, or underscores.", true);
        input.focus();
        return;
      }
      if (channels.some((existing) => existing.toLowerCase() === channel.toLowerCase())) {
        announce(`${channel} is already in this preset.`, true);
        input.select();
        return;
      }
      channels.push(channel);
      input.value = "";
      sync();
      render();
      announce(`${channel} added to the staged preset.`);
      input.focus();
    };

    addButton.addEventListener("click", addChannel);
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      addChannel();
    });
    form.addEventListener("submit", sync);

    source.hidden = true;
    editor.hidden = false;
    render();
  });
})();

(() => {
  "use strict";

  const view = document.querySelector("[data-bot-log-view]");
  if (!view) return;

  const endpoint = view.dataset.logTailUrl;
  const intervalMs = 5000;
  let timer;
  let inFlight = false;

  const schedule = () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(refresh, intervalMs);
  };

  const refresh = async () => {
    if (inFlight || document.hidden) {
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
      if (!response.ok) throw new Error(`Log request failed: ${response.status}`);
      const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
      const incoming = parsed.querySelector("[data-bot-log-fragment]");
      const current = view.querySelector("[data-bot-log-fragment]");
      const currentOutput = current?.querySelector("[data-log-output]");
      const nextOutput = incoming?.querySelector("[data-log-output]");
      if (!incoming || !current || !currentOutput || !nextOutput) throw new Error("Invalid log response");

      const pinned = currentOutput.scrollHeight - currentOutput.scrollTop - currentOutput.clientHeight < 40;
      const priorScroll = currentOutput.scrollTop;
      if (currentOutput.textContent !== nextOutput.textContent) {
        currentOutput.textContent = nextOutput.textContent;
        currentOutput.scrollTop = pinned ? currentOutput.scrollHeight : priorScroll;
      }
      currentOutput.hidden = nextOutput.hidden;

      const currentHealth = current.querySelector("[data-log-health]");
      const nextHealth = incoming.querySelector("[data-log-health]");
      if (currentHealth && nextHealth) currentHealth.replaceChildren(...nextHealth.childNodes);
      const currentEmpty = current.querySelector("[data-log-empty]");
      const nextEmpty = incoming.querySelector("[data-log-empty]");
      if (currentEmpty && nextEmpty) {
        currentEmpty.hidden = nextEmpty.hidden;
        currentEmpty.replaceChildren(...nextEmpty.childNodes);
      }
      const indicator = current.querySelector("[data-log-poll-indicator]");
      if (indicator) indicator.textContent = "Logs updated just now";
    } catch (_error) {
      const indicator = view.querySelector("[data-log-poll-indicator]");
      if (indicator) indicator.textContent = "Log link interrupted";
    } finally {
      inFlight = false;
      schedule();
    }
  };

  const initialOutput = view.querySelector("[data-log-output]");
  if (initialOutput) initialOutput.scrollTop = initialOutput.scrollHeight;
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
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
