(() => {
  "use strict";

  const fit = (container) => {
    const items = [...container.querySelectorAll("[data-channel-item]")];
    const more = container.querySelector("[data-channel-more]");
    if (!more || !items.length) return;

    items.forEach((item) => { item.hidden = false; });
    more.hidden = true;
    if (container.scrollWidth <= container.clientWidth) return;

    more.hidden = false;
    for (let visible = items.length - 1; visible >= 0; visible -= 1) {
      items.forEach((item, index) => { item.hidden = index >= visible; });
      more.textContent = `+${items.length - visible} more`;
      if (container.scrollWidth <= container.clientWidth) break;
    }
  };

  const observer = "ResizeObserver" in window
    ? new ResizeObserver((entries) => entries.forEach(({ target }) => fit(target)))
    : null;

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
        clone.querySelectorAll("[data-channel-overflow]").forEach((container) => {
          container.querySelectorAll("[data-channel-item]").forEach((item) => {
            item.hidden = false;
          });
          const more = container.querySelector("[data-channel-more]");
          if (more) {
            more.hidden = true;
            more.textContent = "";
          }
        });
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

  document.addEventListener("click", (event) => {
    const row = event.target.closest("[data-row-link]");
    if (!row || event.defaultPrevented) return;
    if (event.target.closest("a, button, input, select, textarea, label, form")) return;
    if (
      event.button !== 0
      || event.metaKey
      || event.ctrlKey
      || event.shiftKey
      || event.altKey
    ) return;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) return;
    window.location.assign(row.dataset.rowLink);
  });
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

    const presetSelect = form.querySelector('[data-source-field="preset"] select');
    const presetPreview = form.querySelector("[data-preset-preview]");
    const updatePresetPreview = () => {
      if (!presetSelect || !presetPreview) return;
      const selected = presetSelect.value;
      let matched = false;
      presetPreview.querySelectorAll("[data-preset-option]").forEach((option) => {
        const visible = option.dataset.presetId === selected;
        option.hidden = !visible;
        matched ||= visible;
      });
      const empty = presetPreview.querySelector("[data-preset-empty]");
      if (empty) empty.hidden = matched;
    };

    modeInputs.forEach((input) => input.addEventListener("change", update));
    presetSelect?.addEventListener("change", updatePresetPreview);
    update();
    updatePresetPreview();
  });
})();

(() => {
  "use strict";

  document.querySelectorAll("[data-channel-editor-root]").forEach((root) => {
    const source = root.querySelector("[data-channel-editor-source]");
    const textarea = source?.querySelector("textarea");
    const editor = root.querySelector("[data-channel-editor]");
    const input = editor?.querySelector("[data-channel-input]");
    const addButton = editor?.querySelector("[data-channel-add]");
    const list = editor?.querySelector("[data-channel-list]");
    const feedback = editor?.querySelector("[data-channel-feedback]");
    const verifyUrl = root.dataset.channelVerifyUrl;
    if (!source || !textarea || !editor || !input || !addButton || !list || !feedback) return;

    const seen = new Set();
    let channels = textarea.value
      .split(/[,\r\n]+/)
      .map((value) => value.trim())
      .filter((value) => {
        const key = value.toLocaleLowerCase();
        if (!value || seen.has(key)) return false;
        seen.add(key);
        return true;
      });

    const sync = () => { textarea.value = channels.join("\n"); };
    let verificationPending = false;
    const announce = (message, tone = "neutral") => {
      feedback.textContent = message;
      feedback.dataset.tone = tone;
    };

    const makeRemoveButton = (index) => {
      const button = document.createElement("button");
      button.className = "button button--quiet button--danger";
      button.type = "button";
      button.textContent = "Remove";
      button.dataset.channelAction = "remove";
      button.dataset.channelIndex = String(index);
      button.setAttribute("aria-label", `Remove ${channels[index]}`);
      return button;
    };

    const makeDragHandle = (index) => {
      const handle = document.createElement("button");
      handle.className = "channel-editor__drag-handle";
      handle.type = "button";
      handle.draggable = true;
      handle.textContent = "⠿";
      handle.dataset.channelDragIndex = String(index);
      handle.title = "Drag to change position";
      handle.setAttribute(
        "aria-label",
        `Drag ${channels[index]} to reorder. Use the up and down arrow keys for keyboard reordering.`,
      );
      return handle;
    };

    const render = (focusTarget = null) => {
      list.replaceChildren();
      channels.forEach((channel, index) => {
        const row = document.createElement("li");
        row.className = "channel-editor__row";
        row.dataset.channelIndex = String(index);

        const identity = document.createElement("span");
        identity.className = "channel-editor__identity";
        const position = document.createElement("span");
        position.className = "channel-editor__position";
        position.textContent = String(index + 1);
        position.setAttribute("aria-hidden", "true");
        const tag = document.createElement("span");
        tag.className = "channel-tag";
        tag.textContent = channel;
        identity.append(position, tag);

        const actions = document.createElement("span");
        actions.className = "channel-editor__actions";
        actions.append(makeRemoveButton(index));

        row.append(makeDragHandle(index), identity, actions);
        list.append(row);
      });
      if (!channels.length) {
        const empty = document.createElement("li");
        empty.className = "channel-editor__empty";
        empty.textContent = "No channels staged. Add at least one before saving.";
        list.append(empty);
      }

      if (focusTarget) {
        const selector = focusTarget.action === "drag"
          ? `[data-channel-drag-index="${focusTarget.index}"]`
          : `[data-channel-action="${focusTarget.action}"][data-channel-index="${focusTarget.index}"]`;
        const target = list.querySelector(selector);
        (target || input).focus();
      }
    };

    const clearDropStyles = () => {
      list.querySelectorAll(".channel-editor__row").forEach((row) => {
        row.classList.remove(
          "channel-editor__row--drop-before",
          "channel-editor__row--drop-after",
        );
      });
    };

    const clearDragStyles = () => {
      clearDropStyles();
      list.querySelectorAll(".channel-editor__row--dragging").forEach((row) => {
        row.classList.remove("channel-editor__row--dragging");
      });
    };

    const markDropTarget = (row, clientY) => {
      clearDropStyles();
      const bounds = row.getBoundingClientRect();
      const after = clientY >= bounds.top + bounds.height / 2;
      row.classList.add(
        after
          ? "channel-editor__row--drop-after"
          : "channel-editor__row--drop-before",
      );
      return { index: Number(row.dataset.channelIndex), after };
    };

    const moveChannel = (fromIndex, targetIndex, after) => {
      let nextIndex = targetIndex + (after ? 1 : 0);
      if (fromIndex < nextIndex) nextIndex -= 1;
      nextIndex = Math.max(0, Math.min(nextIndex, channels.length - 1));
      const channel = channels[fromIndex];
      if (nextIndex === fromIndex) {
        clearDragStyles();
        announce(`${channel} remains at position ${fromIndex + 1}.`);
        return;
      }

      channels.splice(fromIndex, 1);
      channels.splice(nextIndex, 0, channel);
      sync();
      render({ action: "drag", index: nextIndex });
      announce(`${channel} moved to position ${nextIndex + 1}.`);
    };

    let nativeDragIndex = null;
    let pointerDrag = null;

    list.addEventListener("dragstart", (event) => {
      const handle = event.target.closest("[data-channel-drag-index]");
      if (!handle) return;
      nativeDragIndex = Number(handle.dataset.channelDragIndex);
      handle.closest(".channel-editor__row")?.classList.add(
        "channel-editor__row--dragging",
      );
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", String(nativeDragIndex));
      }
    });

    list.addEventListener("dragover", (event) => {
      if (nativeDragIndex === null) return;
      const row = event.target.closest(".channel-editor__row");
      if (!row) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      markDropTarget(row, event.clientY);
    });

    list.addEventListener("drop", (event) => {
      if (nativeDragIndex === null) return;
      const row = event.target.closest(".channel-editor__row");
      if (!row) return;
      event.preventDefault();
      const target = markDropTarget(row, event.clientY);
      moveChannel(nativeDragIndex, target.index, target.after);
      nativeDragIndex = null;
      clearDragStyles();
    });

    list.addEventListener("dragend", () => {
      nativeDragIndex = null;
      clearDragStyles();
    });

    list.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse") return;
      const handle = event.target.closest("[data-channel-drag-index]");
      if (!handle) return;
      event.preventDefault();
      handle.setPointerCapture(event.pointerId);
      pointerDrag = {
        handle,
        pointerId: event.pointerId,
        fromIndex: Number(handle.dataset.channelDragIndex),
        target: null,
      };
      handle.closest(".channel-editor__row")?.classList.add(
        "channel-editor__row--dragging",
      );
    });

    list.addEventListener("pointermove", (event) => {
      if (!pointerDrag || pointerDrag.pointerId !== event.pointerId) return;
      event.preventDefault();
      const row = document
        .elementFromPoint(event.clientX, event.clientY)
        ?.closest(".channel-editor__row");
      if (!row || !list.contains(row)) return;
      pointerDrag.target = markDropTarget(row, event.clientY);
    });

    const finishPointerDrag = (event, cancelled = false) => {
      if (!pointerDrag || pointerDrag.pointerId !== event.pointerId) return;
      event.preventDefault();
      const { handle, fromIndex, target } = pointerDrag;
      if (handle.hasPointerCapture(event.pointerId)) {
        handle.releasePointerCapture(event.pointerId);
      }
      pointerDrag = null;
      if (!cancelled && target) {
        moveChannel(fromIndex, target.index, target.after);
      } else {
        clearDragStyles();
      }
    };

    list.addEventListener("pointerup", (event) => finishPointerDrag(event));
    list.addEventListener("pointercancel", (event) => finishPointerDrag(event, true));

    list.addEventListener("keydown", (event) => {
      const handle = event.target.closest("[data-channel-drag-index]");
      if (!handle || !["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      const fromIndex = Number(handle.dataset.channelDragIndex);
      const movingDown = event.key === "ArrowDown";
      const targetIndex = fromIndex + (movingDown ? 1 : -1);
      if (targetIndex < 0 || targetIndex >= channels.length) return;
      moveChannel(fromIndex, targetIndex, movingDown);
    });

    list.addEventListener("click", (event) => {
      const button = event.target.closest("[data-channel-action]");
      if (!button || button.disabled) return;
      const index = Number(button.dataset.channelIndex);
      const channel = channels[index];
      channels.splice(index, 1);
      sync();
      render(
        channels.length
          ? { index: Math.min(index, channels.length - 1), action: "remove" }
          : null,
      );
      announce(`${channel} removed from the staged list.`);
      if (!channels.length) input.focus();
    });

    const addChannel = async () => {
      if (verificationPending) return;
      const channel = input.value.trim();
      if (!/^[A-Za-z0-9_]{1,100}$/.test(channel)) {
        announce("Use 1–100 letters, numbers, or underscores.", "danger");
        input.focus();
        return;
      }
      if (channels.some((existing) => existing.toLowerCase() === channel.toLowerCase())) {
        announce(`${channel} is already in this list.`, "danger");
        input.select();
        return;
      }

      let verificationStatus = "unverified";
      verificationPending = true;
      input.disabled = true;
      addButton.disabled = true;
      announce(`Checking ${channel} on Twitch…`);
      const controller = new AbortController();
      const timeoutId = window.setTimeout(() => controller.abort(), 6000);
      try {
        const endpoint = new URL(verifyUrl, window.location.origin);
        endpoint.searchParams.set("name", channel);
        const response = await fetch(endpoint, {
          cache: "no-store",
          credentials: "same-origin",
          headers: { "X-Requested-With": "XMLHttpRequest" },
          signal: controller.signal,
        });
        if (!response.ok) throw new Error(`Channel check failed: ${response.status}`);
        const payload = await response.json();
        verificationStatus = payload.status;
      } catch (_error) {
        verificationStatus = "unverified";
      } finally {
        window.clearTimeout(timeoutId);
        verificationPending = false;
        input.disabled = false;
        addButton.disabled = false;
      }

      if (verificationStatus === "missing") {
        announce(`${channel} does not exist on Twitch.`, "danger");
        input.select();
        return;
      }

      channels.push(channel);
      input.value = "";
      sync();
      render();
      if (verificationStatus === "exists") {
        announce(`${channel} added at position ${channels.length}.`);
      } else {
        announce(
          `${channel} added, but Twitch could not verify it right now.`,
          "warning",
        );
      }
      input.focus();
    };

    addButton.addEventListener("click", () => { void addChannel(); });
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      void addChannel();
    });
    root.closest("form")?.addEventListener("submit", sync);

    // Django keeps the source textarea required for the no-JavaScript path.
    // Once enhanced, let server validation report an empty staged list instead
    // of letting native validation target a control hidden from the operator.
    textarea.required = false;
    source.hidden = true;
    editor.hidden = false;
    render();
  });
})();

(() => {
  "use strict";
  const summary = document.querySelector("[data-error-summary]");
  if (!summary) return;
  summary.focus();
  summary.addEventListener("click", (event) => {
    const link = event.target.closest('a[href^="#"]');
    if (!link) return;
    const target = document.querySelector(link.getAttribute("href"));
    const editorRoot = target?.closest("[data-channel-editor-root]");
    const editorInput = editorRoot?.querySelector("[data-channel-input]");
    if (target?.closest("[data-channel-editor-source]")?.hidden && editorInput) {
      event.preventDefault();
      editorInput.focus();
    }
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
