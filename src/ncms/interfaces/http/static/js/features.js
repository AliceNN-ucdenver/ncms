/**
 * Features panel — slide-in view of the feature catalog.
 *
 * Responsibilities:
 *   - Consume `features_catalog` from /api/stats (see dashboard.py).
 *   - Update the footer button label (e.g. "8 of 13 features") whenever
 *     app.js refreshes stats.
 *   - Render a grouped, read-only list when the panel is opened.
 *
 * The list is read-only on purpose; toggling features in the running
 * service is a separate admin concern and is not wired up here.
 */

(() => {
  // Keep the last-seen catalog so opening the panel is instant even if
  // the next stats poll hasn't arrived yet.
  let _lastCatalog = null;

  const CATEGORY_ORDER = [
    ["retrieval", "Retrieval"],
    ["ingestion", "Ingestion"],
    ["memory", "Memory Model"],
    ["offline", "Background / Offline"],
  ];

  /**
   * Called by app.js every stats poll.  Updates the footer button and
   * caches the catalog for on-demand rendering.
   */
  function updateFeaturesFromStats(stats) {
    const catalog = Array.isArray(stats?.features_catalog)
      ? stats.features_catalog : null;
    if (!catalog) return;
    _lastCatalog = catalog;

    const enabled = catalog.filter((f) => f.enabled).length;
    const total = catalog.length;
    const countEl = document.getElementById("features-toggle-count");
    if (countEl) countEl.textContent = `${enabled} of ${total}`;

    // If the panel is already open, re-render in place so flips reflect
    // without needing the user to close and reopen.
    const overlay = document.getElementById("features-overlay");
    if (overlay && overlay.classList.contains("open")) {
      _renderPanel(catalog);
    }
  }

  function openFeaturesPanel() {
    const overlay = document.getElementById("features-overlay");
    if (!overlay) return;
    overlay.classList.add("open");
    _renderPanel(_lastCatalog || []);
  }

  function closeFeaturesPanel() {
    const overlay = document.getElementById("features-overlay");
    if (overlay) overlay.classList.remove("open");
  }

  function _renderPanel(catalog) {
    const body = document.getElementById("features-overlay-body");
    const subtitle = document.getElementById("features-overlay-subtitle");
    if (!body) return;

    if (!catalog || catalog.length === 0) {
      body.innerHTML = '<div class="features-empty">No feature metadata yet.</div>';
      if (subtitle) subtitle.textContent = "";
      return;
    }

    const enabled = catalog.filter((f) => f.enabled).length;
    if (subtitle) {
      subtitle.textContent = `${enabled} of ${catalog.length} enabled`;
    }

    // Bucket by category, preserving a stable display order.
    const buckets = new Map();
    catalog.forEach((f) => {
      if (!buckets.has(f.category)) buckets.set(f.category, []);
      buckets.get(f.category).push(f);
    });

    const sections = [];
    for (const [key, label] of CATEGORY_ORDER) {
      const items = buckets.get(key);
      if (!items || items.length === 0) continue;
      sections.push(_renderSection(label, items));
      buckets.delete(key);
    }
    // Any category not in the preferred order goes at the bottom.
    for (const [key, items] of buckets.entries()) {
      const fallback = key
        ? key[0].toUpperCase() + key.slice(1)
        : "Other";
      sections.push(_renderSection(fallback, items));
    }

    body.innerHTML = sections.join("");
  }

  function _renderSection(label, items) {
    const rows = items.map(_renderRow).join("");
    return `
      <div class="features-section">
        <div class="features-section-title">${_escape(label)}</div>
        <div class="features-section-body">${rows}</div>
      </div>
    `;
  }

  function _renderRow(f) {
    const state = f.enabled ? "on" : "off";
    const stateLabel = f.enabled ? "enabled" : "disabled";
    return `
      <div class="features-row features-row--${state}"
           title="${_escape(f.config_key || "")}">
        <span class="features-row-dot"
              aria-label="${stateLabel}"></span>
        <div class="features-row-content">
          <div class="features-row-head">
            <span class="features-row-name">${_escape(f.name)}</span>
            <span class="features-row-state">${stateLabel}</span>
          </div>
          <div class="features-row-desc">${_escape(f.description)}</div>
          ${f.config_key ? `
            <div class="features-row-config">${_escape(f.config_key)}</div>
          ` : ""}
        </div>
      </div>
    `;
  }

  function _escape(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // Esc to close when the panel is open.
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    const overlay = document.getElementById("features-overlay");
    if (overlay && overlay.classList.contains("open")) {
      closeFeaturesPanel();
    }
  });

  // Export to the global scope so app.js can push updates and the
  // inline onclick handlers in index.html can reach us.
  window.updateFeaturesFromStats = updateFeaturesFromStats;
  window.openFeaturesPanel = openFeaturesPanel;
  window.closeFeaturesPanel = closeFeaturesPanel;
})();
