/* global API_BASE */
(function () {
  "use strict";

  const apiBase = typeof API_BASE === "string" ? API_BASE : "";

  /** @type {{ id: string, file: File, env: string, label: string, order: number }[]} */
  let reports = [];
  let sessionId = null;

  const els = {
    dropzone: document.getElementById("dropzone"),
    fileInput: document.getElementById("file-input"),
    reportsBody: document.getElementById("reports-body"),
    reportsEmpty: document.getElementById("reports-empty"),
    scenario: document.getElementById("scenario"),
    symptomList: document.getElementById("symptom-list"),
    slowFields: document.getElementById("slow-query-fields"),
    runBtn: document.getElementById("run-btn"),
    runSpinner: document.getElementById("run-spinner"),
    runHint: document.getElementById("run-hint"),
    errorBanner: document.getElementById("error-banner"),
    resultPanel: document.getElementById("result-panel"),
    statusBar: document.getElementById("status-bar"),
    wikiText: document.getElementById("wiki-text"),
    promptText: document.getElementById("prompt-text"),
    briefText: document.getElementById("brief-text"),
    downloadWiki: document.getElementById("download-wiki"),
    downloadZip: document.getElementById("download-zip"),
    confluenceTitle: document.getElementById("confluence-title"),
    queryHex: document.getElementById("query-hex"),
    queryId: document.getElementById("query-id"),
    queryText: document.getElementById("query-text"),
  };

  function uid() {
    return Math.random().toString(36).slice(2, 10);
  }

  function suggestLabel(filename, env, index) {
    const lower = filename.toLowerCase();
    const prom = lower.match(/prom(\d+)/);
    if (prom) return "prom" + prom[1];
    if (/before/.test(lower)) return "before_settings";
    if (/with_settings|after/.test(lower)) return "after_settings";
    if (/old/.test(lower)) return "old_app";
    if (/prod/.test(lower)) return "prod_" + (index + 1);
    const stem = filename.replace(/\.html$/i, "").replace(/[^\w\-]+/g, "_");
    return stem || (env === "PROD" ? "prod_" : "nt_") + (index + 1);
  }

  function suggestEnv(filename) {
    const lower = filename.toLowerCase();
    if (/prom|prod/.test(lower) && !/_nt_/.test(lower)) return "PROD";
    return "NT";
  }

  function showError(msg) {
    els.errorBanner.textContent = msg || "";
    els.errorBanner.classList.toggle("visible", !!msg);
  }

  function selectedSymptoms() {
    return Array.from(
      els.symptomList.querySelectorAll('input[type="checkbox"]:checked')
    ).map((el) => el.value);
  }

  function updateSlowQueryVisibility() {
    const hasSlow = selectedSymptoms().includes("slow_query");
    els.slowFields.classList.toggle("visible", hasSlow);
  }

  function renderReports() {
    const body = els.reportsBody;
    body.querySelectorAll("tr[data-id]").forEach((tr) => tr.remove());
    if (!reports.length) {
      els.reportsEmpty.style.display = "";
      return;
    }
    els.reportsEmpty.style.display = "none";
    reports
      .slice()
      .sort((a, b) => a.order - b.order)
      .forEach((r) => {
        const tr = document.createElement("tr");
        tr.dataset.id = r.id;
        tr.innerHTML =
          '<td class="filename-cell"></td>' +
          '<td><select class="env-select"><option value="NT">НТ</option><option value="PROD">ПРОМ</option></select></td>' +
          '<td><input class="label-input" type="text"></td>' +
          '<td><input class="order-input" type="text" inputmode="numeric" style="width:4rem"></td>' +
          '<td class="col-actions">' +
          '<button type="button" class="icon-btn btn-up" title="Выше">↑</button> ' +
          '<button type="button" class="icon-btn btn-down" title="Ниже">↓</button> ' +
          '<button type="button" class="icon-btn btn-del" title="Удалить">×</button>' +
          "</td>";
        tr.querySelector(".filename-cell").textContent = r.file.name;
        const envSelect = tr.querySelector(".env-select");
        envSelect.value = r.env;
        envSelect.addEventListener("change", () => {
          r.env = envSelect.value;
        });
        const labelInput = tr.querySelector(".label-input");
        labelInput.value = r.label;
        labelInput.addEventListener("change", () => {
          r.label = labelInput.value.trim() || r.label;
        });
        const orderInput = tr.querySelector(".order-input");
        orderInput.value = String(r.order);
        orderInput.addEventListener("change", () => {
          const n = parseInt(orderInput.value, 10);
          if (!Number.isNaN(n)) r.order = n;
          renderReports();
        });
        tr.querySelector(".btn-del").addEventListener("click", () => {
          reports = reports.filter((x) => x.id !== r.id);
          renderReports();
        });
        tr.querySelector(".btn-up").addEventListener("click", () => {
          const sorted = reports.slice().sort((a, b) => a.order - b.order);
          const idx = sorted.findIndex((x) => x.id === r.id);
          if (idx > 0) {
            const prev = sorted[idx - 1];
            const tmp = r.order;
            r.order = prev.order;
            prev.order = tmp;
            renderReports();
          }
        });
        tr.querySelector(".btn-down").addEventListener("click", () => {
          const sorted = reports.slice().sort((a, b) => a.order - b.order);
          const idx = sorted.findIndex((x) => x.id === r.id);
          if (idx >= 0 && idx < sorted.length - 1) {
            const next = sorted[idx + 1];
            const tmp = r.order;
            r.order = next.order;
            next.order = tmp;
            renderReports();
          }
        });
        body.appendChild(tr);
      });
  }

  function addFiles(fileList) {
    const incoming = Array.from(fileList || []).filter((f) =>
      /\.html?$/i.test(f.name)
    );
    incoming.forEach((file) => {
      const env = suggestEnv(file.name);
      const order = reports.length;
      reports.push({
        id: uid(),
        file,
        env,
        label: suggestLabel(file.name, env, order),
        order,
      });
    });
    renderReports();
    showError("");
  }

  async function loadSymptoms() {
    try {
      const res = await fetch(apiBase + "/api/symptoms");
      const data = await res.json();
      const list = data.symptoms || [];
      els.symptomList.innerHTML = "";
      if (!list.length) {
        els.symptomList.innerHTML =
          '<p class="empty-row" style="padding:0.5rem 0;">симптомы не найдены</p>';
        return;
      }
      list.forEach((s) => {
        const label = document.createElement("label");
        label.className = "symptom-item";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = s.id;
        input.addEventListener("change", updateSlowQueryVisibility);
        const wrap = document.createElement("div");
        const strong = document.createElement("strong");
        strong.textContent = s.title || s.id;
        const span = document.createElement("span");
        const desc = (s.description || "").split("\n")[0];
        span.textContent = desc;
        wrap.appendChild(strong);
        wrap.appendChild(span);
        label.appendChild(input);
        label.appendChild(wrap);
        els.symptomList.appendChild(label);
      });
    } catch (err) {
      els.symptomList.innerHTML =
        '<p class="alert alert-error">не удалось загрузить симптомы</p>';
    }
  }

  function setTabs() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.getAttribute("data-tab");
        document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
        document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
        btn.classList.add("active");
        const panel = document.getElementById("tab-" + tab);
        if (panel) panel.classList.add("active");
      });
    });
  }

  async function copyText(text) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      els.runHint.textContent = "скопировано";
      setTimeout(() => {
        if (els.runHint.textContent === "скопировано") els.runHint.textContent = "";
      }, 1500);
    } catch (_) {
      showError("не удалось скопировать — выделите текст вручную");
    }
  }

  function showResult(data) {
    sessionId = data.session_id;
    els.resultPanel.classList.add("visible");
    els.wikiText.value = data.wiki_text || "";
    els.promptText.value = data.prompt_text || "";
    els.briefText.value = data.brief_text || "";

    const summary = data.summary || {};
    const pills = [];
    pills.push(
      '<span class="status-pill">сценарий <strong>' +
        (data.scenario || "") +
        "</strong></span>"
    );
    if (summary.total_findings != null) {
      pills.push(
        '<span class="status-pill">findings <strong>' +
          summary.total_findings +
          "</strong></span>"
      );
    }
    if (summary.symptoms && summary.symptoms.length) {
      pills.push(
        '<span class="status-pill">симптомы <strong>' +
          summary.symptoms.join(", ") +
          "</strong></span>"
      );
    }
    if (summary.nt_runs_symptoms && summary.nt_runs_symptoms.length) {
      pills.push(
        '<span class="status-pill">симптомы <strong>' +
          summary.nt_runs_symptoms.join(", ") +
          "</strong></span>"
      );
    }
    if (summary.symptom) {
      pills.push(
        '<span class="status-pill">confirmed <strong>' +
          (summary.symptom.confirmed_count || 0) +
          "</strong> · suspected <strong>" +
          (summary.symptom.suspected_count || 0) +
          "</strong></span>"
      );
    }
    if (data.wiki) {
      pills.push(
        '<span class="status-pill">wiki <strong>' + data.wiki + "</strong></span>"
      );
    }
    els.statusBar.innerHTML = pills.join("");

    els.downloadWiki.href = apiBase + "/api/sessions/" + sessionId + "/wiki";
    els.downloadWiki.download = data.wiki || "confluence.wiki";
    els.downloadZip.href = apiBase + "/api/sessions/" + sessionId + "/zip";
  }

  async function runAnalysis() {
    showError("");
    if (!reports.length) {
      showError("добавьте хотя бы один HTML-отчёт");
      return;
    }
    const symptoms = selectedSymptoms();
    const scenario = els.scenario.value;

    els.runBtn.disabled = true;
    els.runSpinner.classList.add("visible");
    els.runHint.textContent = "анализ…";

    const sorted = reports.slice().sort((a, b) => a.order - b.order);
    const meta = {
      scenario: scenario,
      symptoms: symptoms,
      confluence_title: els.confluenceTitle.value.trim() || null,
      query_hex: els.queryHex.value.trim() || null,
      query_id: els.queryId.value.trim() || null,
      query_text: els.queryText.value.trim() || null,
      reports: sorted.map((r) => ({
        filename: r.file.name,
        env: r.env,
        label: r.label,
        order: r.order,
      })),
    };

    const form = new FormData();
    form.append("meta", JSON.stringify(meta));
    sorted.forEach((r) => form.append("file", r.file, r.file.name));

    try {
      const res = await fetch(apiBase + "/api/analyze", {
        method: "POST",
        body: form,
      });
      const data = await res.json();
      if (!res.ok) {
        showError(data.error || "ошибка анализа");
        els.runHint.textContent = "";
        return;
      }
      showResult(data);
      els.runHint.textContent = "готово";
    } catch (err) {
      showError(String(err.message || err));
      els.runHint.textContent = "";
    } finally {
      els.runBtn.disabled = false;
      els.runSpinner.classList.remove("visible");
    }
  }

  // dropzone
  els.dropzone.addEventListener("click", () => els.fileInput.click());
  els.dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      els.fileInput.click();
    }
  });
  els.fileInput.addEventListener("change", () => {
    addFiles(els.fileInput.files);
    els.fileInput.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) => {
    els.dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      els.dropzone.classList.add("dragover");
    });
  });
  ["dragleave", "drop"].forEach((ev) => {
    els.dropzone.addEventListener(ev, (e) => {
      e.preventDefault();
      els.dropzone.classList.remove("dragover");
    });
  });
  els.dropzone.addEventListener("drop", (e) => {
    addFiles(e.dataTransfer.files);
  });

  els.runBtn.addEventListener("click", runAnalysis);
  document.getElementById("copy-wiki").addEventListener("click", () =>
    copyText(els.wikiText.value)
  );
  document.getElementById("copy-prompt").addEventListener("click", () =>
    copyText(els.promptText.value)
  );

  setTabs();
  loadSymptoms();
})();
