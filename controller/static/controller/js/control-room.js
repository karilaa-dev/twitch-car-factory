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
