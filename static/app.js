const state = {
  stats: null,
  trends: null,
  page: 1,
  limit: 40,
  hasMore: false,
};

const $ = (selector) => document.querySelector(selector);

async function fetchJson(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function optionList(items, placeholder) {
  return [`<option value="">${placeholder}</option>`]
    .concat(items.map((item) => `<option value="${escapeHtml(item.label)}">${escapeHtml(item.label)} (${item.count})</option>`))
    .join("");
}

async function boot() {
  state.stats = await fetchJson("/api/stats");
  state.trends = await fetchJson("/api/trends");
  const topics = await fetchJson("/api/topics");

  $("#docCount").textContent = state.stats.documents.toLocaleString("es-ES");
  $("#materiaFilter").innerHTML = optionList(state.stats.materias, "Todas");
  $("#baseFilter").innerHTML = optionList(state.stats.bases, "Todas");
  $("#regimenFilter").innerHTML = optionList(state.stats.regimes, "Todos");

  renderTimeline(state.stats.years);
  renderRankList("#commonTopics", topics.common);
  renderRankList("#rareTopics", topics.rare);
  renderChangeCandidates();
  bindEvents();
  await runSearch();
}

function bindEvents() {
  ["queryInput", "materiaFilter", "baseFilter", "regimenFilter", "yearFrom", "yearTo"].forEach((id) => {
    $(`#${id}`).addEventListener("input", debounce(() => runSearch({ resetPage: true }), 250));
    $(`#${id}`).addEventListener("change", () => runSearch({ resetPage: true }));
  });
  $("#refreshButton").addEventListener("click", () => runSearch());
  $("#prevPageButton").addEventListener("click", () => {
    if (state.page > 1) {
      state.page -= 1;
      runSearch();
    }
  });
  $("#nextPageButton").addEventListener("click", () => {
    if (state.hasMore) {
      state.page += 1;
      runSearch();
    }
  });
  $("#analyzeTreatmentButton").addEventListener("click", analyzeTreatment);
}

async function analyzeTreatment() {
  const description = $("#treatmentInput").value.trim();
  const target = $("#treatmentAnalysis");
  if (!description) {
    target.innerHTML = `<div class="empty">Describe primero el tratamiento de datos.</div>`;
    return;
  }
  target.innerHTML = `<div class="empty">Analizando tratamiento y buscando doctrina relacionada...</div>`;
  const analysis = await postJson("/api/analyze-treatment", { description, limit: 14 });
  if (analysis.error) {
    target.innerHTML = `<div class="empty">${escapeHtml(analysis.error)}</div>`;
    return;
  }
  renderTreatmentAnalysis(analysis);
}

function renderTreatmentAnalysis(analysis) {
  const target = $("#treatmentAnalysis");
  target.innerHTML = `
    <div class="analysis-verdict ${escapeHtml(analysis.orientation.label.replaceAll(" ", "-"))}">
      <div>
        <span>Orientacion</span>
        <strong>${escapeHtml(analysis.orientation.label)}</strong>
        <p>${escapeHtml(analysis.orientation.rationale)}</p>
      </div>
      <div class="confidence">
        <span>Confianza</span>
        <strong>${escapeHtml(analysis.orientation.confidence)}</strong>
      </div>
    </div>
    <div class="analysis-detected">
      <span class="chip">${escapeHtml(analysis.detected.materia)}</span>
      <span class="chip">${escapeHtml(analysis.detected.base_legal)}</span>
      <span class="chip">${escapeHtml(analysis.detected.regimen)}</span>
      ${analysis.terms.map((term) => `<span class="chip">${escapeHtml(term)}</span>`).join("")}
    </div>
    <div class="analysis-section">
      <h4>Riesgos y cautelas</h4>
      <ul>${analysis.risks.map((risk) => `<li>${escapeHtml(risk)}</li>`).join("")}</ul>
    </div>
    <div class="analysis-columns">
      ${reportGroup("Informes que apoyan", analysis.supporting_reports)}
      ${reportGroup("Informes que cuestionan", analysis.questioning_reports)}
      ${reportGroup("Informes condicionados", analysis.conditional_reports)}
    </div>
    <p class="analysis-disclaimer">${escapeHtml(analysis.disclaimer)}</p>
  `;
  target.querySelectorAll(".analysis-report").forEach((card) => {
    card.addEventListener("click", () => loadDocument(card.dataset.reference));
  });
}

function reportGroup(title, reports) {
  const body = reports.length
    ? reports.slice(0, 5).map(analysisReportCard).join("")
    : `<div class="empty">No se han localizado informes en esta categoria.</div>`;
  return `<section class="analysis-section"><h4>${title}</h4>${body}</section>`;
}

function analysisReportCard(report) {
  return `
    <article class="analysis-report" data-reference="${escapeHtml(report.reference)}">
      <div>
        <strong>${escapeHtml(report.reference)}</strong>
        <span>${report.year || "s/f"} · ${escapeHtml(report.regimen)}</span>
      </div>
      <p>${escapeHtml(report.summary || report.snippet.replace(/<[^>]+>/g, "")).slice(0, 260)}...</p>
      <div class="meta">
        <span class="chip">${escapeHtml(report.stance)}</span>
        <span class="chip">vigencia ${escapeHtml(report.vigencia.level)} · ${report.vigencia.score}</span>
      </div>
    </article>
  `;
}

async function runSearch(options = {}) {
  if (options.resetPage) state.page = 1;
  const params = new URLSearchParams();
  const fields = {
    q: $("#queryInput").value,
    materia: $("#materiaFilter").value,
    base_legal: $("#baseFilter").value,
    regimen: $("#regimenFilter").value,
    year_from: $("#yearFrom").value,
    year_to: $("#yearTo").value,
    page: String(state.page),
    limit: String(state.limit),
  };
  Object.entries(fields).forEach(([key, value]) => {
    if (value) params.set(key, value);
  });
  const data = await fetchJson(`/api/search?${params.toString()}`);
  state.page = data.page || state.page;
  state.hasMore = Boolean(data.has_more);
  const termText = data.main_terms?.length ? ` · palabras principales: ${data.main_terms.join(", ")}` : "";
  const rangeStart = data.results.length ? (state.page - 1) * state.limit + 1 : 0;
  const rangeEnd = (state.page - 1) * state.limit + data.results.length;
  $("#resultMeta").textContent = `${rangeStart}-${rangeEnd} resultados${termText}`;
  renderResults(data.results);
  renderPagination();
}

function renderResults(results) {
  const container = $("#results");
  if (!results.length) {
    container.innerHTML = `<div class="empty">No hay resultados con esos filtros.</div>`;
    return;
  }
  container.innerHTML = results.map(resultCard).join("");
  container.querySelectorAll(".result-card").forEach((card) => {
    card.addEventListener("click", () => loadDocument(card.dataset.reference));
  });
}

function renderPagination() {
  $("#pageIndicator").textContent = `Pagina ${state.page}`;
  $("#prevPageButton").disabled = state.page <= 1;
  $("#nextPageButton").disabled = !state.hasMore;
}

function resultCard(doc) {
  const snippet = doc.snippet || "";
  const searchQuality = searchQualityBlock(doc);
  return `
    <article class="result-card" data-reference="${escapeHtml(doc.reference)}">
      <h4>${escapeHtml(doc.reference)} · ${escapeHtml(doc.title)}</h4>
      <div class="meta">
        <span class="chip">${doc.year || "s/f"}</span>
        <span class="chip">${escapeHtml(doc.materia)}</span>
        <span class="chip">${escapeHtml(doc.base_legal)}</span>
        <span class="chip">${escapeHtml(doc.regimen)}</span>
        ${doc.doctrinal_change_score ? `<span class="chip">doctrina ${doc.doctrinal_change_score}</span>` : ""}
      </div>
      ${searchQuality}
      <div class="snippet">${snippet || `${doc.page_count} paginas · ${doc.char_count.toLocaleString("es-ES")} caracteres`}</div>
    </article>
  `;
}

function searchQualityBlock(doc) {
  if (!doc.search_terms || !doc.search_terms.length) return "";
  const complete = doc.match_label === "completa";
  const missing = doc.missing_terms || [];
  const matched = doc.matched_terms || [];
  return `
    <div class="search-quality ${complete ? "complete" : "partial"}">
      <span>${complete ? "coincidencia completa" : "coincidencia parcial"} · ${matched.length}/${doc.search_terms.length}</span>
      ${missing.length ? `<small>faltan: ${missing.map(escapeHtml).join(", ")}</small>` : `<small>contiene todas las palabras principales</small>`}
    </div>
  `;
}

async function loadDocument(reference) {
  const params = new URLSearchParams();
  const activeQuery = $("#queryInput").value.trim();
  if (activeQuery) params.set("q", activeQuery);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  const doc = await fetchJson(`/api/document/${encodeURIComponent(reference)}${suffix}`);
  const pdfUrl = `/pdf/${encodeURIComponent(doc.reference)}.pdf`;
  $("#detail").innerHTML = `
    <div class="detail-head">
      <h4>${escapeHtml(doc.reference)}</h4>
      <a class="pdf-button" href="${pdfUrl}" target="_blank" rel="noopener">Ver PDF entero</a>
    </div>
    <div class="path">${escapeHtml(doc.path)}</div>
    <div class="meta">
      <span class="chip">${doc.year || "s/f"}</span>
      <span class="chip">${escapeHtml(doc.materia)}</span>
      <span class="chip">${escapeHtml(doc.base_legal)}</span>
      <span class="chip">${escapeHtml(doc.regimen)}</span>
      <span class="chip">${doc.page_count} paginas</span>
    </div>
    <p>${escapeHtml(doc.title)}</p>
    ${summaryBlock(doc.summary, doc.summary_variants || [], doc.reference)}
    <div class="fragment-title">Fragmentos destacados</div>
    ${doc.chunks.map(keyChunk).join("")}
  `;
  const llmButton = $("#generateLlmSummaryButton");
  if (llmButton) {
    llmButton.addEventListener("click", () => generateLlmSummary(doc.reference));
  }
  bindSummaryVariants();
}

function summaryBlock(summary, variants, reference) {
  if (!summary) return "";
  const summaries = [summary, ...variants];
  const selector = summaries.length > 1
    ? `<div class="summary-provider-tabs" role="tablist" aria-label="Proveedor del resumen">
        ${summaries.map((item, index) => `
          <button
            class="summary-provider-tab ${index === 0 ? "active" : ""}"
            type="button"
            role="tab"
            aria-selected="${index === 0 ? "true" : "false"}"
            data-summary-index="${index}"
          >${escapeHtml(summaryProviderName(item))}</button>
        `).join("")}
      </div>`
    : "";
  return `
    <section class="summary">
      <div class="summary-head">
        <div>
          <h5>Resumen del dictamen</h5>
        </div>
        <button
          id="generateLlmSummaryButton"
          class="summary-action"
          type="button"
          data-reference="${escapeHtml(reference)}"
        >${String(summary.method || "").startsWith("openai:") ? "Regenerar con OpenAI" : "Mejorar con OpenAI"}</button>
      </div>
      ${selector}
      <div id="summaryContent">
        ${summaryContentMarkup(summary)}
      </div>
      <script id="summaryVariantsData" type="application/json">${safeJsonForHtml(summaries)}</script>
    </section>
  `;
}

function summaryContentMarkup(summary) {
  const isLlm = /^(openai|nvidia):/.test(String(summary.method || ""));
  return `
      <span class="summary-source ${isLlm ? "llm" : ""}">${escapeHtml(summarySourceLabel(summary))}</span>
      <p>${escapeHtml(summary.overview)}</p>
      <div class="summary-columns">
        <div>
          <strong>Puntos principales</strong>
          <ul>${(summary.key_points || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </div>
        <div>
          <strong>Conclusiones</strong>
          <ul>${(summary.conclusions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </div>
      </div>
  `;
}

function bindSummaryVariants() {
  const dataNode = $("#summaryVariantsData");
  if (!dataNode) return;
  const summaries = JSON.parse(dataNode.textContent);
  document.querySelectorAll(".summary-provider-tab").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.summaryIndex);
      $("#summaryContent").innerHTML = summaryContentMarkup(summaries[index]);
      document.querySelectorAll(".summary-provider-tab").forEach((tab) => {
        const active = tab === button;
        tab.classList.toggle("active", active);
        tab.setAttribute("aria-selected", String(active));
      });
    });
  });
}

function summaryProviderName(summary) {
  const method = String(summary.method || "");
  if (method.startsWith("openai:")) return "OpenAI";
  if (method.startsWith("nvidia:")) return "NVIDIA";
  return "Extractivo";
}

function summarySourceLabel(summary) {
  const provider = summaryProviderName(summary);
  if (provider === "Extractivo") return "Resumen extractivo";
  const model = summary.model || String(summary.method || "").split(":").slice(1).join(":");
  return `${provider} · ${model}`;
}

function safeJsonForHtml(value) {
  return JSON.stringify(value)
    .replaceAll("&", "\\u0026")
    .replaceAll("<", "\\u003c")
    .replaceAll(">", "\\u003e");
}

async function generateLlmSummary(reference) {
  const button = $("#generateLlmSummaryButton");
  if (!button) return;
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "Generando...";
  try {
    await postJson(`/api/document/${encodeURIComponent(reference)}/llm-summary`, {});
    await loadDocument(reference);
  } catch (error) {
    button.disabled = false;
    button.textContent = originalLabel;
    window.alert(`No se pudo generar el resumen: ${error.message}`);
  }
}

function keyChunk(chunk) {
  const reasons = (chunk.interest_reasons || []).map((reason) => `<span class="chip">${escapeHtml(reason)}</span>`).join("");
  return `
    <article class="chunk">
      <div class="chunk-meta">
        <strong>Fragmento ${Number(chunk.chunk_index) + 1}</strong>
        <span>interes ${chunk.interest_score}</span>
      </div>
      <div class="meta">${reasons}</div>
      <p>${escapeHtml(chunk.text.slice(0, 1200))}${chunk.text.length > 1200 ? "..." : ""}</p>
    </article>
  `;
}

function renderTimeline(years) {
  const canvas = $("#timeline");
  const ctx = canvas.getContext("2d");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.max(300, Math.floor(rect.width)) * ratio;
  canvas.height = 190 * ratio;
  ctx.scale(ratio, ratio);

  const width = canvas.width / ratio;
  const height = canvas.height / ratio;
  const compact = width < 560;
  const padding = { top: compact ? 42 : 30, right: 12, bottom: 34, left: 34 };
  const max = Math.max(...years.map((item) => item.count), 1);
  const barWidth = (width - padding.left - padding.right) / years.length;

  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = "#687782";
  ctx.font = "12px Inter, sans-serif";
  ctx.fillText(String(max), 4, padding.top + 8);

  years.forEach((item, index) => {
    const x = padding.left + index * barWidth;
    const h = ((height - padding.top - padding.bottom) * item.count) / max;
    const y = height - padding.bottom - h;
    ctx.fillStyle = item.year >= 2018 ? "#0f766e" : "#315f86";
    ctx.fillRect(x + 2, y, Math.max(3, barWidth - 4), h);

    const countLabel = String(item.count);
    ctx.save();
    ctx.fillStyle = "#40505b";
    ctx.font = `${compact ? 9 : 11}px Inter, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    if (compact) {
      ctx.translate(x + barWidth / 2, Math.max(18, y - 5));
      ctx.rotate(-Math.PI / 2);
      ctx.fillText(countLabel, 0, 0);
    } else {
      ctx.fillText(countLabel, x + barWidth / 2, Math.max(12, y - 9));
    }
    ctx.restore();

    if (index % 3 === 0 || index === years.length - 1) {
      ctx.save();
      ctx.translate(x + 2, height - 12);
      ctx.rotate(-Math.PI / 5);
      ctx.fillStyle = "#687782";
      ctx.fillText(String(item.year), 0, 0);
      ctx.restore();
    }
  });
}

function renderRankList(selector, items) {
  $(selector).innerHTML = items
    .map((item) => `<div class="rank-row"><span>${escapeHtml(item.label)}</span><strong>${item.count}</strong></div>`)
    .join("");
}

function renderChangeCandidates() {
  const target = $("#detail");
  if (!state.stats.change_candidates.length) return;
  target.innerHTML = `
    <div class="detail">
      <h4>Candidatos a cambio doctrinal</h4>
      ${state.stats.change_candidates
        .slice(0, 5)
        .map(
          (doc) => `
          <article class="result-card" data-reference="${escapeHtml(doc.reference)}">
            <h4>${escapeHtml(doc.reference)}</h4>
            <div class="meta"><span class="chip">${doc.year}</span><span class="chip">${escapeHtml(doc.materia)}</span><span class="chip">score ${doc.doctrinal_change_score}</span></div>
            <p>${escapeHtml(doc.title)}</p>
          </article>`
        )
        .join("")}
    </div>
  `;
  target.querySelectorAll(".result-card").forEach((card) => card.addEventListener("click", () => loadDocument(card.dataset.reference)));
}

function debounce(fn, wait) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

boot().catch((error) => {
  document.body.innerHTML = `<main class="shell"><section class="panel"><h2>No se pudo cargar la base de datos</h2><p>${escapeHtml(error.message)}</p><p>Ejecuta primero la ingesta desde la terminal.</p></section></main>`;
});
