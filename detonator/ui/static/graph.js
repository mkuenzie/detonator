// Graph page client — search, render, expand.
// Talks to /graph/search and /graph/nodes/{node_type}/{id}/neighbors on the
// main API. Cytoscape dedupes by id so repeated expansions merge cleanly.

(function () {
  "use strict";

  const NODE_COLORS = {
    observable: "#3b82f6", // blue
    technique:  "#f59e0b", // amber
    campaign:   "#10b981", // green
  };

  const cy = cytoscape({
    container: document.getElementById("cy"),
    elements: [],
    wheelSensitivity: 0.2,
    style: [
      {
        selector: "node",
        style: {
          "background-color": (el) => NODE_COLORS[el.data("node_type")] || "#888",
          "label": "data(label)",
          "color": "#fff",
          "text-valign": "center",
          "text-halign": "center",
          "text-outline-width": 2,
          "text-outline-color": (el) => NODE_COLORS[el.data("node_type")] || "#888",
          "font-size": 11,
          "text-wrap": "ellipsis",
          "text-max-width": "140px",
          "width": 46,
          "height": 46,
        },
      },
      {
        selector: "node.center",
        style: { "border-width": 3, "border-color": "#111" },
      },
      {
        selector: "node:selected",
        style: { "border-width": 4, "border-color": "#ef4444" },
      },
      {
        selector: "edge",
        style: {
          "width": 1.5,
          "line-color": "#94a3b8",
          "target-arrow-color": "#94a3b8",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          "label": "data(label)",
          "font-size": 9,
          "color": "#475569",
          "text-background-color": "#fff",
          "text-background-opacity": 0.85,
          "text-background-padding": 2,
        },
      },
    ],
  });

  // ── Layout ---------------------------------------------------------

  function relayout() {
    cy.layout({
      name: "cose",
      animate: false,
      nodeRepulsion: 8000,
      idealEdgeLength: 120,
      padding: 30,
    }).run();
  }

  // ── Fetch + merge --------------------------------------------------

  async function expandNode(nodeType, entityId) {
    const url = `/graph/nodes/${encodeURIComponent(nodeType)}/${encodeURIComponent(entityId)}/neighbors`;
    const resp = await fetch(url);
    if (!resp.ok) {
      console.error("neighbors fetch failed", resp.status);
      return;
    }
    const data = await resp.json();
    // cy.add() is additive; duplicate ids are silently rejected.
    cy.add(data.nodes);
    cy.add(data.edges);
    relayout();
    const center = cy.getElementById(data.center_id);
    if (center && center.nonempty()) {
      cy.animate({ center: { eles: center }, duration: 300 });
    }
  }

  // ── Search ---------------------------------------------------------

  const searchInput = document.getElementById("node-search");
  const resultsBox  = document.getElementById("search-results");
  let searchTimer = null;

  function renderResults(rows) {
    if (!rows.length) {
      resultsBox.innerHTML = '<p class="muted">No matches.</p>';
      resultsBox.hidden = false;
      return;
    }
    const html = rows.map((r) => `
      <button type="button" class="search-result"
              data-node-type="${r.node_type}" data-entity-id="${r.id}">
        <span class="legend-chip legend-${r.node_type}">${r.node_type}</span>
        <span class="result-label">${escapeHtml(r.label)}</span>
        <span class="muted">${escapeHtml(r.sublabel || "")}</span>
      </button>
    `).join("");
    resultsBox.innerHTML = html;
    resultsBox.hidden = false;
  }

  async function runSearch(q) {
    if (!q) {
      resultsBox.hidden = true;
      resultsBox.innerHTML = "";
      return;
    }
    const resp = await fetch(`/graph/search?q=${encodeURIComponent(q)}&limit=20`);
    if (!resp.ok) {
      console.error("search failed", resp.status);
      return;
    }
    renderResults(await resp.json());
  }

  searchInput.addEventListener("input", (e) => {
    clearTimeout(searchTimer);
    const q = e.target.value.trim();
    searchTimer = setTimeout(() => runSearch(q), 200);
  });

  resultsBox.addEventListener("click", (e) => {
    const btn = e.target.closest(".search-result");
    if (!btn) return;
    const nodeType = btn.dataset.nodeType;
    const entityId = btn.dataset.entityId;
    expandNode(nodeType, entityId);
    loadDetail(nodeType, entityId);
    resultsBox.hidden = true;
    searchInput.value = "";
  });

  // ── Canvas interactions -------------------------------------------

  cy.on("tap", "node", (evt) => {
    const n = evt.target;
    expandNode(n.data("node_type"), n.data("entity_id"));
    loadDetail(n.data("node_type"), n.data("entity_id"));
  });

  cy.on("dbltap", "node", (evt) => {
    const n = evt.target;
    const nodeType = n.data("node_type");
    const entityId = n.data("entity_id");
    // Deep-link to the existing detail pages.
    if (nodeType === "observable") {
      window.open(`/ui/observables/${entityId}`, "_blank");
    } else if (nodeType === "campaign") {
      // No dedicated campaign detail page yet; open the API view.
      window.open(`/campaigns/${entityId}`, "_blank");
    } else if (nodeType === "technique") {
      window.open(`/techniques/${entityId}/matches`, "_blank");
    }
  });

  // ── Detail panel ---------------------------------------------------

  const detailBox = document.getElementById("node-detail");

  async function loadDetail(nodeType, entityId) {
    detailBox.innerHTML = '<p class="muted">Loading…</p>';
    try {
      let html = "";
      if (nodeType === "observable") {
        const d = await (await fetch(`/observables/${entityId}`)).json();
        html = `
          <h4>Observable</h4>
          <p><strong>${escapeHtml(d.value || "")}</strong>
             <span class="muted">(${escapeHtml(d.type || "")})</span></p>
          <p class="muted">First: ${escapeHtml(d.first_seen || "—")}<br>
             Last: ${escapeHtml(d.last_seen || "—")}</p>
          <p><strong>Runs:</strong> ${(d.runs || []).length}</p>
          <p><strong>Outgoing links:</strong> ${(d.outgoing_links || []).length} ·
             <strong>Incoming:</strong> ${(d.incoming_links || []).length}</p>
          <p><strong>Campaigns:</strong> ${(d.campaigns || []).length}</p>
          <p><a href="/ui/observables/${entityId}" target="_blank">Open detail page →</a></p>
        `;
      } else if (nodeType === "campaign") {
        const d = await (await fetch(`/campaigns/${entityId}`)).json();
        html = `
          <h4>Campaign</h4>
          <p><strong>${escapeHtml(d.name || "")}</strong>
             <span class="muted">(${escapeHtml(d.status || "")})</span></p>
          <p class="muted">${escapeHtml(d.description || "")}</p>
          <p><strong>Runs:</strong> ${(d.runs || []).length} ·
             <strong>Observables:</strong> ${(d.observables || []).length} ·
             <strong>Techniques:</strong> ${(d.techniques || []).length}</p>
        `;
      } else if (nodeType === "technique") {
        const matches = await (await fetch(`/techniques/${entityId}/matches`)).json();
        html = `
          <h4>Technique</h4>
          <p>${matches.length} run match(es)</p>
          <ul class="detail-list">
            ${matches.slice(0, 10).map((m) => `
              <li><a href="/ui/runs/${m.run_id}">${escapeHtml(m.seed_url || m.run_id)}</a>
                  <span class="muted">conf ${Number(m.confidence || 0).toFixed(2)}</span></li>
            `).join("")}
          </ul>
        `;
      }
      detailBox.innerHTML = html || '<p class="muted">No detail available.</p>';
    } catch (err) {
      console.error(err);
      detailBox.innerHTML = '<p class="muted">Error loading detail.</p>';
    }
  }

  // ── Reset ---------------------------------------------------------

  document.getElementById("graph-reset").addEventListener("click", () => {
    cy.elements().remove();
    detailBox.innerHTML = '<p class="muted">Canvas cleared.</p>';
  });

  // ── Utils ---------------------------------------------------------

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
})();
