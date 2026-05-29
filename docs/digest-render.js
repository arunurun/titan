/** Sector digest HTML rendering for insights.html (loaded before app.js). */
(function () {
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function normalizeDigestAction(text) {
    const u = String(text || "").toUpperCase();
    if (u.includes("EXIT RISK") || u.includes("EXIT-RISK") || /\bEXIT\b/.test(u)) return "exit-risk";
    if (/\bSELL\b/.test(u)) return "exit-risk";
    if (/\bTRIM\b/.test(u)) return "trim";
    if (/\bBUY\b/.test(u)) return "buy";
    if (/\bHOLD\b/.test(u)) return "hold";
    return "";
  }

  function digestCardClass(action) {
    if (action === "buy") return "action-buy";
    if (action === "hold") return "action-hold";
    if (action === "trim" || action === "exit-risk") return "action-sell";
    return "action-neutral";
  }

  function badgeClass(action) {
    if (action === "buy") return "badge-buy";
    if (action === "hold") return "badge-hold";
    if (action === "trim" || action === "exit-risk") return "badge-sell";
    return "badge-neutral";
  }

  function formatDigestNumbers(line) {
    return String(line).replace(/(-?\d+\.\d{3,})/g, (n) => {
      const x = Number(n);
      return Number.isFinite(x) ? x.toFixed(2) : n;
    });
  }

  function renderSymbolHeadline(line) {
    const trimmed = String(line || "").trim();
    const m = trimmed.match(
      /^(.+?\((?:NSE|BSE)\))\s*[—–-]\s*([^—–-]+?)(?:\s*[—–-]\s*(.*))?$/i,
    );
    if (!m) return escapeHtml(trimmed);
    const sym = m[1].trim();
    const actionLabel = m[2].trim();
    const rest = (m[3] || "").trim();
    const bCls = badgeClass(normalizeDigestAction(actionLabel));
    let html = `<span class="sym">${escapeHtml(sym)}</span> `;
    html += `<span class="badge ${bCls}">${escapeHtml(actionLabel)}</span>`;
    if (rest) html += ` <span class="rest">— ${escapeHtml(formatDigestNumbers(rest))}</span>`;
    return html;
  }

  function isSymbolActionHead(line) {
    return /^[A-Z0-9&][A-Z0-9&.\-]{0,24}\s*\((?:NSE|BSE)\)\s*[—–-]/i.test(String(line || "").trim());
  }

  function isSymbolSnapshotLine(line) {
    return /^[A-Z0-9&][A-Z0-9&.\-]{0,24}\s*\((?:NSE|BSE)\)\s*·/i.test(String(line || "").trim());
  }

  function parseDigestSections(text) {
    const sections = [];
    let current = { title: null, lines: [] };
    for (const ln of String(text || "").split(/\r?\n/)) {
      const t = ln.trimEnd();
      const hdr = t.trim().match(/^---\s*(.+?)\s*---\s*$/);
      if (hdr) {
        if (current.title || current.lines.length) sections.push(current);
        current = { title: hdr[1], lines: [] };
      } else if (t.trim() || current.lines.length) {
        current.lines.push(t);
      }
    }
    if (current.title || current.lines.length) sections.push(current);
    return sections;
  }

  function renderSymbolCardsHtml(lines) {
    const blocks = [];
    let preamble = [];
    let block = null;
    for (const ln of lines) {
      const t = ln.trim();
      if (!t) continue;
      if (isSymbolActionHead(t)) {
        if (block) blocks.push(block);
        block = { head: t, lines: [] };
      } else if (block) {
        block.lines.push(t);
      } else {
        preamble.push(t);
      }
    }
    if (block) blocks.push(block);

    let html = "";
    if (preamble.length) {
      html += `<div class="digest-prose digest-intro">${preamble
        .map((p) => `<p>${escapeHtml(formatDigestNumbers(p))}</p>`)
        .join("")}</div>`;
    }
    for (const b of blocks) {
      const cardCls = digestCardClass(normalizeDigestAction(b.head));
      html += `<div class="digest-card ${cardCls}">`;
      html += `<div class="head">${renderSymbolHeadline(b.head)}</div>`;
      for (const sub of b.lines) {
        html += `<div class="line">${escapeHtml(formatDigestNumbers(sub))}</div>`;
      }
      html += "</div>";
    }
    return html;
  }

  function renderInsightMeta(runMeta, insight, sectorLabel) {
    const host = document.getElementById("insightDigestMeta");
    if (!host) return;
    const rows = [];
    if (runMeta.github_run_number != null) {
      rows.push(
        `<div class="meta-row"><span class="meta-k">GitHub run</span><span class="meta-v">#${escapeHtml(
          String(runMeta.github_run_number),
        )} <span class="muted">(${escapeHtml(String(runMeta.github_run_id ?? ""))})</span></span></div>`,
      );
    }
    if (runMeta.workflow_mode) {
      rows.push(
        `<div class="meta-row"><span class="meta-k">Mode</span><span class="meta-v">${escapeHtml(
          runMeta.workflow_mode,
        )}</span></div>`,
      );
    }
    if (insight?.recorded_at) {
      rows.push(
        `<div class="meta-row"><span class="meta-k">Recorded</span><span class="meta-v">${escapeHtml(
          insight.recorded_at,
        )}</span></div>`,
      );
    }
    const lab = sectorLabel || insight?.sector || "";
    if (lab) {
      rows.push(
        `<div class="meta-row"><span class="meta-k">Sector</span><span class="meta-v">${escapeHtml(lab)}</span></div>`,
      );
    }
    if (insight?.run_id) {
      rows.push(
        `<div class="meta-row"><span class="meta-k">Digest id</span><span class="meta-v"><code>${escapeHtml(
          insight.run_id,
        )}</code></span></div>`,
      );
    }
    host.innerHTML = rows.length ? `<div class="digest-meta-card">${rows.join("")}</div>` : "";
  }

  function renderInsightDigestHtml(digestText) {
    const host = document.getElementById("insightDigestHtml");
    if (!host) return;
    const raw = String(digestText || "").trim();
    if (!raw) {
      host.innerHTML = '<p class="digest-empty muted">No digest body.</p>';
      return;
    }

    const sections = parseDigestSections(raw);
    let html = "";

    const lead = sections[0];
    if (lead && !lead.title && lead.lines.length) {
      html += `<div class="digest-lead">${lead.lines
        .map((p) => `<p>${escapeHtml(formatDigestNumbers(p.trim()))}</p>`)
        .join("")}</div>`;
    }

    for (const sec of sections) {
      if (!sec.title && sec === lead) continue;
      const title = sec.title;
      const lines = sec.lines.map((l) => l.trim()).filter(Boolean);
      if (!lines.length && !title) continue;

      if (title) {
        html += `<h4 class="digest-section-title">${escapeHtml(title)}</h4>`;
      }

      const isMetrics = title && /per-symbol|symbol metrics|constituent/i.test(title);
      const hasActionHeads = lines.some((l) => isSymbolActionHead(l));

      if (isMetrics || hasActionHeads) {
        html += renderSymbolCardsHtml(lines);
      } else if (
        title &&
        /executive|snapshot|movement|buckets|risk|quality|reconciliation|data|prediction/i.test(title)
      ) {
        html += `<div class="digest-kv">${lines
          .map((p) => `<div class="kv-line">${escapeHtml(formatDigestNumbers(p))}</div>`)
          .join("")}</div>`;
      } else {
        html += `<div class="digest-prose">${lines
          .map((p) => {
            if (isSymbolSnapshotLine(p)) {
              const action = normalizeDigestAction(p);
              const snapCls = action ? `snapshot-line ${digestCardClass(action)}` : "snapshot-line";
              return `<p class="${snapCls}">${escapeHtml(formatDigestNumbers(p))}</p>`;
            }
            return `<p>${escapeHtml(formatDigestNumbers(p))}</p>`;
          })
          .join("")}</div>`;
      }
    }

    if (!html) {
      html = renderSymbolCardsHtml(raw.split(/\r?\n/));
    }

    host.innerHTML = html || `<pre class="digest-fallback">${escapeHtml(formatDigestNumbers(raw))}</pre>`;
  }

  window.TitanDigestRender = {
    escapeHtml,
    renderInsightMeta,
    renderInsightDigestHtml,
  };
})();
