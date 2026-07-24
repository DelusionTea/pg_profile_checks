/* global API_BASE, WikiPreview */
(function () {
  "use strict";

  const apiBase = typeof API_BASE === "string" ? API_BASE : "";
  const JVM_PROBLEM_REQUIRED_FIELDS = {
    gc_latency: ["gc_pause_p95_ms"],
    memory_pressure: ["container_memory_usage_percent"],
    heap_pressure: ["heap_used_mib", "heap_used_percent", "old_gen_used_percent"],
  };

  /** @type {{ id: string, file: File, env: string, label: string, order: number }[]} */
  let reports = [];
  /** @type {{ id: string, file: File }[]} */
  let jvmFiles = [];
  /** @type {{ pod_name: string, container_name: string, display_name: string }[]} */
  let jvmTargets = [];
  let jvmLastInput = null;
  let currentMode = "pg_profile";
  let sessionId = null;
  let lastWikiText = "";
  let severityFilter = null;

  const SCENARIO_HELP = {
    auto: "Авто: симптомы+≥2 НТ → nt_runs; симптомы → symptom; ≥2 файлов → полный анализ; 1 файл → health.",
    full_multi: "Health по каждому файлу → общие findings + специфичные по отчётам.",
    symptom: "Точечное расследование выбранных симптомов (playbook + evidence).",
    nt_runs: "Несколько прогонов НТ: симптомы, влияние GUC, опционально PROD baseline.",
    health: "Пороги thresholds.yaml по одному отчёту + рекомендации.",
    stable_prod: "Общие проблемы на нескольких PROD (или всех) отчётах + GUC tuning.",
    nt_prod: "Gate НТ vs ПРОМ: settings + метрики.",
    compare_runs: "Два отчёта: health первого + diff метрик и Defined settings.",
  };

  const els = {
    modeToggleButtons: document.querySelectorAll(".mode-toggle"),
    dropzone: document.getElementById("dropzone"),
    reportPanel: document.getElementById("report-panel"),
    dropzoneTitle: document.querySelector("#dropzone .dropzone-title"),
    dropzoneHint: document.getElementById("dropzone-hint"),
    fileInput: document.getElementById("file-input"),
    jvmDropzone: document.getElementById("jvm-dropzone"),
    jvmFileInput: document.getElementById("jvm-file-input"),
    reportsBody: document.getElementById("reports-body"),
    reportsTable: document.getElementById("reports-table"),
    reportsEmpty: document.getElementById("reports-empty"),
    reportsHeadSimple: document.getElementById("reports-head-simple"),
    reportsHeadAdvanced: document.getElementById("reports-head-advanced"),
    advancedSettings: document.getElementById("advanced-settings"),
    jvmAdvancedSettings: document.getElementById("jvm-advanced-settings"),
    simpleModeNote: document.getElementById("simple-mode-note"),
    jvmFilesNote: document.getElementById("jvm-files-note"),
    jvmFilesList: document.getElementById("jvm-files-list"),
    jvmFields: document.getElementById("jvm-fields"),
    jvmSystemName: document.getElementById("jvm-system-name"),
    jvmPodName: document.getElementById("jvm-pod-name"),
    jvmContainerName: document.getElementById("jvm-container-name"),
    jvmProblemList: document.getElementById("jvm-problem-list"),
    jvmThresholdProfile: document.getElementById("jvm-threshold-profile"),
    jvmJdkVersion: document.getElementById("jvm-jdk-version"),
    jvmSpringBootVersion: document.getElementById("jvm-spring-boot-version"),
    jvmGcP95: document.getElementById("jvm-gc-p95"),
    jvmGcP99: document.getElementById("jvm-gc-p99"),
    jvmGcRatio: document.getElementById("jvm-gc-ratio"),
    jvmMemoryUsagePercent: document.getElementById("jvm-memory-usage-percent"),
    jvmHeapUsed: document.getElementById("jvm-heap-used"),
    jvmHeapUsedPercent: document.getElementById("jvm-heap-used-percent"),
    jvmOldgenUsed: document.getElementById("jvm-oldgen-used"),
    jvmOldgenCapacity: document.getElementById("jvm-oldgen-capacity"),
    jvmOldgenUsedPercent: document.getElementById("jvm-oldgen-used-percent"),
    jvmNewgenUsedMib: document.getElementById("jvm-newgen-used-mib"),
    jvmNewgenCapacityMib: document.getElementById("jvm-newgen-capacity-mib"),
    jvmNewgenUsedPercent: document.getElementById("jvm-newgen-used-percent"),
    jvmFillLastValuesBtn: document.getElementById("jvm-fill-last-values-btn"),
    jvmHistoryHint: document.getElementById("jvm-history-hint"),
    scenario: document.getElementById("scenario"),
    scenarioHelp: document.getElementById("scenario-help"),
    autoPreview: document.getElementById("auto-scenario-preview"),
    symptomList: document.getElementById("symptom-list"),
    slowFields: document.getElementById("slow-query-fields"),
    runBtn: document.getElementById("run-btn"),
    runSpinner: document.getElementById("run-spinner"),
    runHint: document.getElementById("run-hint"),
    appTitle: document.getElementById("app-title"),
    appSubtitle: document.getElementById("app-subtitle"),
    simpleAnalysisHint: document.getElementById("simple-analysis-hint"),
    errorBanner: document.getElementById("error-banner"),
    toast: document.getElementById("toast"),
    resultPanel: document.getElementById("result-panel"),
    statusBar: document.getElementById("status-bar"),
    findingsCards: document.getElementById("findings-cards"),
    checkFlow: document.getElementById("check-flow"),
    wikiText: document.getElementById("wiki-text"),
    wikiPreview: document.getElementById("wiki-preview"),
    promptText: document.getElementById("prompt-text"),
    briefText: document.getElementById("brief-text"),
    downloadWiki: document.getElementById("download-wiki"),
    downloadZip: document.getElementById("download-zip"),
    confluenceTitle: document.getElementById("confluence-title"),
    queryHex: document.getElementById("query-hex"),
    queryId: document.getElementById("query-id"),
    queryText: document.getElementById("query-text"),
  };

  function isAdvancedMode() {
    return !!(els.advancedSettings && els.advancedSettings.open);
  }

  function isJvmMode() {
    return currentMode === "jvm";
  }

  function selectedJvmProblems() {
    if (!els.jvmProblemList) return [];
    return Array.from(
      els.jvmProblemList.querySelectorAll('input[type="checkbox"]:checked')
    ).map((el) => el.value);
  }

  function jvmMetricMeta() {
    return {
      gc_pause_p95_ms: _numberOrNull(els.jvmGcP95 && els.jvmGcP95.value),
      gc_pause_p99_ms: _numberOrNull(els.jvmGcP99 && els.jvmGcP99.value),
      gc_time_ratio_percent: _numberOrNull(els.jvmGcRatio && els.jvmGcRatio.value),
      container_memory_usage_percent: _numberOrNull(
        els.jvmMemoryUsagePercent && els.jvmMemoryUsagePercent.value
      ),
      heap_used_mib: _numberOrNull(els.jvmHeapUsed && els.jvmHeapUsed.value),
      heap_used_percent: _numberOrNull(els.jvmHeapUsedPercent && els.jvmHeapUsedPercent.value),
      old_gen_used_mib: _numberOrNull(els.jvmOldgenUsed && els.jvmOldgenUsed.value),
      old_gen_capacity_mib: _numberOrNull(els.jvmOldgenCapacity && els.jvmOldgenCapacity.value),
      old_gen_used_percent: _numberOrNull(
        els.jvmOldgenUsedPercent && els.jvmOldgenUsedPercent.value
      ),
      new_gen_used_mib: _numberOrNull(els.jvmNewgenUsedMib && els.jvmNewgenUsedMib.value),
      new_gen_capacity_mib: _numberOrNull(
        els.jvmNewgenCapacityMib && els.jvmNewgenCapacityMib.value
      ),
      new_gen_used_percent: _numberOrNull(
        els.jvmNewgenUsedPercent && els.jvmNewgenUsedPercent.value
      ),
    };
  }

  function hasAnyJvmMetric(meta) {
    return Object.values(meta || {}).some((v) => typeof v === "number");
  }

  function hasRequiredJvmMetrics(meta) {
    return (
      meta.gc_pause_p95_ms != null &&
      meta.heap_used_mib != null &&
      meta.container_memory_usage_percent != null
    );
  }

  function hasPodChoices() {
    return jvmTargets.some((target) => !!(target.pod_name || "").trim());
  }

  function validateJvmProblemInputs(selectedProblems, meta) {
    const missing = [];
    selectedProblems.forEach((pid) => {
      const required = JVM_PROBLEM_REQUIRED_FIELDS[pid] || [];
      const nonePresent = required.length && required.every((k) => meta[k] == null);
      if (nonePresent) {
        missing.push(pid);
      }
    });
    return missing;
  }

  function _setJvmMetricValue(input, value) {
    if (!input) return;
    input.value = value == null ? "" : String(value);
  }

  function applyLastJvmValues(values) {
    if (!values) return;
    _setJvmMetricValue(els.jvmGcP95, values.gc_pause_p95_ms);
    _setJvmMetricValue(els.jvmGcP99, values.gc_pause_p99_ms);
    _setJvmMetricValue(els.jvmGcRatio, values.gc_time_ratio_percent);
    _setJvmMetricValue(els.jvmMemoryUsagePercent, values.container_memory_usage_percent);
    _setJvmMetricValue(els.jvmHeapUsed, values.heap_used_mib);
    _setJvmMetricValue(els.jvmHeapUsedPercent, values.heap_used_percent);
    _setJvmMetricValue(els.jvmOldgenUsed, values.old_gen_used_mib);
    _setJvmMetricValue(els.jvmOldgenCapacity, values.old_gen_capacity_mib);
    _setJvmMetricValue(els.jvmOldgenUsedPercent, values.old_gen_used_percent);
    _setJvmMetricValue(els.jvmNewgenUsedMib, values.new_gen_used_mib);
    _setJvmMetricValue(els.jvmNewgenCapacityMib, values.new_gen_capacity_mib);
    _setJvmMetricValue(els.jvmNewgenUsedPercent, values.new_gen_used_percent);
  }

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

  function showToast(msg) {
    if (!els.toast) return;
    els.toast.textContent = msg;
    els.toast.classList.add("visible");
    setTimeout(() => els.toast.classList.remove("visible"), 1800);
  }

  function selectedSymptoms() {
    return Array.from(
      els.symptomList.querySelectorAll('input[type="checkbox"]:checked')
    ).map((el) => el.value);
  }

  function updateSlowQueryVisibility() {
    const hasSlow = selectedSymptoms().includes("slow_query");
    els.slowFields.classList.toggle("visible", hasSlow);
    updateScenarioHints();
  }

  function suggestAutoScenario() {
    const symptoms = selectedSymptoms();
    const nt = reports.filter((r) => r.env === "NT").length;
    if (symptoms.length) {
      if (nt >= 2) return "nt_runs";
      return "symptom";
    }
    if (reports.length >= 2) return "full_multi";
    if (reports.length === 1) return "health";
    return "auto";
  }

  function updateModeUi() {
    if (els.modeToggleButtons) {
      els.modeToggleButtons.forEach((btn) => {
        btn.classList.toggle("active", btn.getAttribute("data-mode") === currentMode);
      });
    }
    if (isJvmMode()) {
      if (els.appTitle) els.appTitle.textContent = "JVM CHECKS";
      if (els.appSubtitle) {
        els.appSubtitle.textContent =
          "// ресурсы АС · проблемы · impact · рекомендации";
      }
      if (els.simpleAnalysisHint) {
        els.simpleAnalysisHint.textContent =
          "Анализ JVM проблем выбранного контейнера: оценка влияния, рисков и практических шагов исправления.";
      }
      if (els.reportPanel) els.reportPanel.hidden = true;
      if (els.dropzone) els.dropzone.hidden = true;
      if (els.reportsTable) els.reportsTable.hidden = true;
      if (els.simpleModeNote) els.simpleModeNote.hidden = true;
      if (els.advancedSettings) els.advancedSettings.hidden = true;
      if (els.jvmFields) els.jvmFields.hidden = false;
      if (els.jvmAdvancedSettings) els.jvmAdvancedSettings.hidden = false;
      if (els.jvmFilesList) {
        els.jvmFilesList.textContent = jvmFiles.length
          ? "Загружены файлы: " + jvmFiles.map((f) => f.file.name).join(", ")
          : "Файлы обновления не загружены.";
      }
      if (els.runBtn) {
        const metricMeta = jvmMetricMeta();
        const requiredReady = hasRequiredJvmMetrics(metricMeta);
        const ready = !!(
          els.jvmSystemName &&
          els.jvmSystemName.value &&
          (!hasPodChoices() || (els.jvmPodName && els.jvmPodName.value)) &&
          els.jvmContainerName &&
          els.jvmContainerName.value &&
          requiredReady
        );
        els.runBtn.disabled = !ready;
      }
      if (els.jvmFillLastValuesBtn) {
        els.jvmFillLastValuesBtn.disabled = !jvmLastInput;
      }
      if (els.jvmHistoryHint) {
        const hasSystem = !!(els.jvmSystemName && els.jvmSystemName.value);
        const hasContainer = !!(els.jvmContainerName && els.jvmContainerName.value);
        if (!hasSystem || !hasContainer) {
          els.jvmHistoryHint.textContent = "выберите АС и контейнер для истории";
        } else if (jvmLastInput) {
          const updatedAt = jvmLastInput.updated_at ? " · " + jvmLastInput.updated_at : "";
          els.jvmHistoryHint.textContent = "история найдена" + updatedAt;
        } else {
          els.jvmHistoryHint.textContent =
            "история для выбранной АС/контейнера не найдена";
        }
      }
      if (els.runHint) {
        const metricMeta = jvmMetricMeta();
        const selectedProblems = selectedJvmProblems();
        const requiredReady = hasRequiredJvmMetrics(metricMeta);
        const missingByProblem = validateJvmProblemInputs(selectedProblems, metricMeta);
        if (!els.jvmSystemName || !els.jvmSystemName.value) {
          els.runHint.textContent = "выберите АС";
        } else if (hasPodChoices() && (!els.jvmPodName || !els.jvmPodName.value)) {
          els.runHint.textContent = "выберите pod";
        } else if (!els.jvmContainerName || !els.jvmContainerName.value) {
          els.runHint.textContent = "выберите контейнер";
        } else if (!requiredReady) {
          els.runHint.textContent = "заполните GC p95, Heap used (MiB), Memory usage (%)";
        } else if (missingByProblem.length) {
          els.runHint.textContent =
            "для отмеченных проблем заполните обязательные значения";
        } else {
          els.runHint.textContent = "";
        }
      }
      return;
    }
    if (els.dropzone) els.dropzone.hidden = false;
    if (els.reportPanel) els.reportPanel.hidden = false;
    if (els.appTitle) els.appTitle.textContent = "PG PROFILE CHECKS";
    if (els.appSubtitle) {
      els.appSubtitle.textContent = "// отчёты · health-check · confluence";
    }
    if (els.simpleAnalysisHint) {
      els.simpleAnalysisHint.textContent =
        "Полный health-check одного отчёта: checkpoints, WAL, cache, sessions, memory, IO, autovacuum, locks и др. Результат — Confluence wiki с чеклистом PASS / FAIL / SUSPECT.";
    }
    const adv = isAdvancedMode();
    if (els.reportsTable) els.reportsTable.hidden = false;
    if (els.advancedSettings) els.advancedSettings.hidden = false;
    if (els.jvmAdvancedSettings) els.jvmAdvancedSettings.hidden = true;
    if (els.jvmFields) els.jvmFields.hidden = true;
    if (els.jvmFillLastValuesBtn) els.jvmFillLastValuesBtn.disabled = true;
    if (els.jvmHistoryHint) els.jvmHistoryHint.textContent = "";
    if (els.dropzoneTitle) {
      els.dropzoneTitle.textContent = "Перетащите HTML отчёт pg_profile";
    }
    if (els.fileInput) {
      els.fileInput.setAttribute("accept", ".html,text/html");
    }
    if (els.reportsHeadSimple) els.reportsHeadSimple.hidden = adv;
    if (els.reportsHeadAdvanced) els.reportsHeadAdvanced.hidden = !adv;
    if (els.dropzoneHint) {
      els.dropzoneHint.textContent = adv
        ? "или нажмите, чтобы выбрать файлы · можно несколько"
        : "или нажмите, чтобы выбрать файл · полный health-check одного отчёта";
    }
    if (els.simpleModeNote) {
      els.simpleModeNote.hidden = adv;
      if (!adv && reports.length > 1) {
        els.simpleModeNote.textContent =
          "Будет проанализирован только первый файл («" +
          reports.slice().sort((a, b) => a.order - b.order)[0].file.name +
          "»). Откройте расширенные настройки для мульти-отчётов.";
      } else if (!adv) {
        els.simpleModeNote.textContent =
          "По умолчанию анализируется только первый файл (все категории health-check). Мульти-отчёты и симптомы — в расширенных настройках.";
      }
    }
    if (els.reportsEmpty) {
      els.reportsEmpty.querySelector("td").colSpan = adv ? 5 : 2;
      els.reportsEmpty.querySelector("td").textContent = adv
        ? "файлы ещё не добавлены"
        : "файл ещё не добавлен";
    }
  }

  function updateScenarioHints() {
    updateModeUi();
    if (isJvmMode()) {
      if (els.scenarioHelp) {
        els.scenarioHelp.textContent = "Для check jvm сценарий не используется.";
      }
      if (els.autoPreview) {
        els.autoPreview.hidden = true;
      }
      return;
    }
    const sc = els.scenario.value;
    if (els.scenarioHelp) {
      els.scenarioHelp.textContent = SCENARIO_HELP[sc] || SCENARIO_HELP.auto;
    }
    if (els.autoPreview) {
      if (isAdvancedMode() && sc === "auto" && reports.length) {
        const sug = suggestAutoScenario();
        els.autoPreview.hidden = false;
        els.autoPreview.textContent = "Авто выберет: " + sug;
      } else {
        els.autoPreview.hidden = true;
      }
    }
    els.runBtn.disabled = !reports.length;
    els.runHint.textContent = reports.length ? "" : "добавьте отчёт";
  }

  function renderReports() {
    const body = els.reportsBody;
    const adv = isAdvancedMode();
    body.querySelectorAll("tr[data-id]").forEach((tr) => tr.remove());
    if (!reports.length) {
      els.reportsEmpty.style.display = "";
      updateScenarioHints();
      return;
    }
    els.reportsEmpty.style.display = "none";
    const sorted = reports.slice().sort((a, b) => a.order - b.order);
    sorted.forEach((r, idx) => {
      const tr = document.createElement("tr");
      tr.dataset.id = r.id;
      if (!adv) {
        const used = idx === 0;
        tr.innerHTML =
          '<td class="filename-cell"></td>' +
          '<td class="col-actions">' +
          (used
            ? '<span class="status-pill">анализ</span> '
            : '<span class="status-pill">пропуск</span> ') +
          '<button type="button" class="icon-btn btn-del" title="Удалить">×</button>' +
          "</td>";
        tr.querySelector(".filename-cell").textContent = r.file.name;
        tr.querySelector(".btn-del").addEventListener("click", () => {
          reports = reports.filter((x) => x.id !== r.id);
          renderReports();
        });
        body.appendChild(tr);
        return;
      }
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
        updateScenarioHints();
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
        const list = reports.slice().sort((a, b) => a.order - b.order);
        const i = list.findIndex((x) => x.id === r.id);
        if (i > 0) {
          const prev = list[i - 1];
          const tmp = r.order;
          r.order = prev.order;
          prev.order = tmp;
          renderReports();
        }
      });
      tr.querySelector(".btn-down").addEventListener("click", () => {
        const list = reports.slice().sort((a, b) => a.order - b.order);
        const i = list.findIndex((x) => x.id === r.id);
        if (i >= 0 && i < list.length - 1) {
          const next = list[i + 1];
          const tmp = r.order;
          r.order = next.order;
          next.order = tmp;
          renderReports();
        }
      });
      body.appendChild(tr);
    });
    updateScenarioHints();
  }

  function addFiles(fileList) {
    const incoming = Array.from(fileList || []).filter((f) =>
      /\.html?$/i.test(f.name)
    );
    if (!isAdvancedMode() && incoming.length) {
      // Simple mode: keep a single report (last selected wins if replacing).
      if (!reports.length) {
        const file = incoming[0];
        const env = suggestEnv(file.name);
        reports = [
          {
            id: uid(),
            file,
            env,
            label: suggestLabel(file.name, env, 0),
            order: 0,
          },
        ];
        if (incoming.length > 1) {
          showToast("простой режим: взят первый файл");
        }
      } else {
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
        showToast("будет использован только первый файл");
      }
    } else {
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
    }
    renderReports();
    showError("");
  }

  function addJvmFiles(fileList) {
    const incoming = Array.from(fileList || []).filter((f) =>
      /\.(yaml|yml|txt)$/i.test(f.name)
    );
    incoming.forEach((file) => {
      jvmFiles.push({ id: uid(), file: file });
    });
    updateModeUi();
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

  async function loadJvmSystems() {
    if (!els.jvmSystemName) return;
    try {
      const res = await fetch(apiBase + "/api/jvm/systems");
      const data = await res.json();
      const systems = data.systems || [];
      els.jvmSystemName.innerHTML = "";
      if (!systems.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "системы не найдены";
        els.jvmSystemName.appendChild(opt);
        updateModeUi();
        return;
      }
      const first = document.createElement("option");
      first.value = "";
      first.textContent = "выберите систему";
      els.jvmSystemName.appendChild(first);
      systems.forEach((name) => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name === "__root__" ? "(resources root)" : name;
        els.jvmSystemName.appendChild(opt);
      });
      await loadJvmContainers();
      updateModeUi();
    } catch (err) {
      els.jvmSystemName.innerHTML = '<option value="">ошибка загрузки систем</option>';
      updateModeUi();
    }
  }

  async function loadJvmProblems() {
    if (!els.jvmProblemList) return;
    try {
      const res = await fetch(apiBase + "/api/jvm/problems");
      const data = await res.json();
      const problems = data.problems || [];
      els.jvmProblemList.innerHTML = "";
      if (!problems.length) {
        els.jvmProblemList.innerHTML =
          '<p class="empty-row" style="padding:0.5rem 0;">проблемы не найдены</p>';
        return;
      }
      problems.forEach((p) => {
        const label = document.createElement("label");
        label.className = "symptom-item";
        const input = document.createElement("input");
        input.type = "checkbox";
        input.value = p.id;
        input.addEventListener("change", updateModeUi);
        const wrap = document.createElement("div");
        const strong = document.createElement("strong");
        strong.textContent = p.title || p.id;
        const span = document.createElement("span");
        span.textContent = p.description || "";
        wrap.appendChild(strong);
        wrap.appendChild(span);
        label.appendChild(input);
        label.appendChild(wrap);
        els.jvmProblemList.appendChild(label);
      });
    } catch (err) {
      els.jvmProblemList.innerHTML =
        '<p class="alert alert-error">не удалось загрузить JVM проблемы</p>';
    }
  }

  async function loadJvmContainers() {
    if (!els.jvmContainerName || !els.jvmSystemName) return;
    const systemName = els.jvmSystemName.value || "";
    if (!systemName) {
      if (els.jvmPodName) {
        els.jvmPodName.innerHTML = '<option value="">сначала выберите АС</option>';
      }
      els.jvmContainerName.innerHTML = '<option value="">сначала выберите АС</option>';
      jvmTargets = [];
      updateModeUi();
      return;
    }
    try {
      const res = await fetch(
        apiBase + "/api/jvm/containers?system=" + encodeURIComponent(systemName)
      );
      const data = await res.json();
      const rawTargets = data.containers || [];
      jvmTargets = rawTargets.map((item) => {
        if (typeof item === "string") {
          return { pod_name: "", container_name: item, display_name: item };
        }
        return {
          pod_name: item.pod_name || "",
          container_name: item.container_name || "",
          display_name:
            item.display_name ||
            ((item.pod_name ? item.pod_name + " / " : "") + (item.container_name || "")),
        };
      });
      renderJvmPodAndContainerSelectors();
      updateModeUi();
    } catch (err) {
      if (els.jvmPodName) {
        els.jvmPodName.innerHTML = '<option value="">ошибка загрузки pod</option>';
      }
      els.jvmContainerName.innerHTML = '<option value="">ошибка загрузки контейнеров</option>';
      jvmTargets = [];
      updateModeUi();
    }
  }

  function renderJvmPodAndContainerSelectors() {
    if (!els.jvmContainerName) return;
    const pods = Array.from(
      new Set(
        jvmTargets
          .map((target) => (target.pod_name || "").trim())
          .filter((name) => !!name)
      )
    ).sort((a, b) => a.localeCompare(b));

    const previousPod = els.jvmPodName ? els.jvmPodName.value : "";
    if (els.jvmPodName) {
      els.jvmPodName.innerHTML = "";
      const firstPod = document.createElement("option");
      firstPod.value = "";
      firstPod.textContent = pods.length ? "выберите pod" : "pod не требуется";
      els.jvmPodName.appendChild(firstPod);
      pods.forEach((podName) => {
        const opt = document.createElement("option");
        opt.value = podName;
        opt.textContent = podName;
        els.jvmPodName.appendChild(opt);
      });
      if (previousPod && pods.includes(previousPod)) {
        els.jvmPodName.value = previousPod;
      }
    }

    const selectedPod = (els.jvmPodName && els.jvmPodName.value) || "";
    const filtered = pods.length && !selectedPod
      ? []
      : jvmTargets.filter((target) => !selectedPod || target.pod_name === selectedPod);
    const names = Array.from(
      new Set(filtered.map((target) => (target.container_name || "").trim()).filter(Boolean))
    ).sort((a, b) => a.localeCompare(b));

    const previousContainer = els.jvmContainerName.value;
    els.jvmContainerName.innerHTML = "";
    const first = document.createElement("option");
    first.value = "";
    if (pods.length && !selectedPod) {
      first.textContent = "сначала выберите pod";
    } else {
      first.textContent = names.length ? "выберите контейнер" : "контейнеры не найдены";
    }
    els.jvmContainerName.appendChild(first);
    names.forEach((name) => {
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = name;
      els.jvmContainerName.appendChild(opt);
    });
    if (previousContainer && names.includes(previousContainer)) {
      els.jvmContainerName.value = previousContainer;
    }
  }

  async function loadJvmLastInput() {
    jvmLastInput = null;
    const hasSystem = !!(els.jvmSystemName && els.jvmSystemName.value);
    const hasContainer = !!(els.jvmContainerName && els.jvmContainerName.value);
    if (!hasSystem || !hasContainer) {
      updateModeUi();
      return;
    }
    try {
      const res = await fetch(
        apiBase +
          "/api/jvm/last-input?system=" +
          encodeURIComponent(els.jvmSystemName.value) +
          "&pod=" +
          encodeURIComponent((els.jvmPodName && els.jvmPodName.value) || "") +
          "&container=" +
          encodeURIComponent(els.jvmContainerName.value)
      );
      if (!res.ok) {
        updateModeUi();
        return;
      }
      const data = await res.json();
      jvmLastInput = data.values || null;
    } catch (_) {
      jvmLastInput = null;
    }
    updateModeUi();
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

  async function copyText(text, okMsg) {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      showToast(okMsg || "скопировано");
    } catch (_) {
      showError("не удалось скопировать — выделите текст вручную");
    }
  }

  function extractVerdictAndActions(wiki) {
    const lines = String(wiki || "").split(/\r?\n/);
    const out = [];
    let mode = "head";
    let actionCount = 0;
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      if (mode === "head") {
        out.push(line);
        if (/^h2\.\s+Что сделать/.test(line)) {
          mode = "actions";
        }
        continue;
      }
      if (mode === "actions") {
        if (/^h2\./.test(line) && !/^h2\.\s+Что сделать/.test(line)) break;
        out.push(line);
        if (/^# /.test(line)) actionCount += 1;
        if (actionCount >= 12) break;
      }
    }
    return out.join("\n").trim() + "\n";
  }

  function setWikiMode(mode) {
    document.querySelectorAll(".wiki-mode").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-mode") === mode);
    });
    const isPreview = mode === "preview";
    els.wikiText.hidden = isPreview;
    els.wikiPreview.hidden = !isPreview;
    if (isPreview && window.WikiPreview) {
      els.wikiPreview.innerHTML = WikiPreview.render(lastWikiText);
    }
  }

  function thresholdLink(fid) {
    if (!fid || fid.indexOf(".") < 0) return null;
    const section = fid.split(".")[0];
    const map = {
      checkpoints: "checkpoints",
      wal: "wal",
      queries: "queries",
      autovacuum: "autovacuum",
      cache: "cache",
      sessions: "sessions",
      memory: "memory",
      io: "io",
      disk: "disk",
      locks: "locks",
      db: "io",
    };
    const sec = map[section];
    if (!sec) return null;
    return "/thresholds#sec-" + encodeURIComponent(sec);
  }

  function renderFindingsCards(findings, filterSev) {
    const root = els.findingsCards;
    if (!root) return;
    let list = findings || [];
    if (filterSev) {
      list = list.filter((f) => {
        const s = String(f.severity || "").toLowerCase();
        if (filterSev === "critical") return s === "critical" || s === "high";
        if (filterSev === "warning") return s === "warning" || s === "medium";
        if (filterSev === "info") return s === "info" || s === "low";
        return true;
      });
    }
    if (!list.length) {
      root.hidden = !(findings && findings.length);
      root.innerHTML = findings && findings.length
        ? '<p class="empty-row">нет findings для фильтра</p>'
        : "";
      return;
    }
    root.hidden = false;
    root.innerHTML = list
      .slice(0, 40)
      .map((f) => {
        const sev = String(f.severity || "warning").toLowerCase();
        const link = thresholdLink(f.id);
        const thr = f.threshold
          ? '<div class="finding-threshold">порог: <code>' +
            escapeHtml(f.threshold) +
            "</code></div>"
          : "";
        const thrLink = link
          ? ' <a class="finding-thr-link" href="' +
            link +
            '">thresholds</a>'
          : "";
        return (
          '<article class="finding-card sev-' +
          escapeHtml(sev) +
          '">' +
          '<div class="finding-card-head">' +
          '<span class="sev-badge">' +
          escapeHtml(sev) +
          "</span>" +
          "<code>" +
          escapeHtml(f.id || "?") +
          "</code>" +
          thrLink +
          "</div>" +
          '<p class="finding-title">' +
          escapeHtml(f.title || f.id || "") +
          "</p>" +
          '<p class="finding-msg">' +
          escapeHtml((f.message || "").slice(0, 220)) +
          "</p>" +
          (f.advice
            ? '<p class="finding-advice">' + escapeHtml(f.advice) + "</p>"
            : "") +
          thr +
          "</article>"
        );
      })
      .join("");
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function bindCheckFlow(session) {
    if (!els.checkFlow) return;
    els.checkFlow.hidden = false;
    const key = "pgprofile_checkflow_" + (session || "x");
    let saved = {};
    try {
      saved = JSON.parse(sessionStorage.getItem(key) || "{}");
    } catch (_) {
      saved = {};
    }
    els.checkFlow.querySelectorAll("input[type=checkbox]").forEach((cb) => {
      const step = cb.getAttribute("data-step");
      cb.checked = !!saved[step];
      cb.onchange = () => {
        saved[step] = cb.checked;
        try {
          sessionStorage.setItem(key, JSON.stringify(saved));
        } catch (_) {}
      };
    });
  }

  function showResult(data) {
    sessionId = data.session_id;
    els.resultPanel.classList.add("visible");
    lastWikiText = data.wiki_text || "";
    els.wikiText.value = lastWikiText;
    els.promptText.value = data.prompt_text || "";
    els.briefText.value = data.brief_text || "";
    setWikiMode("source");

    const summary = data.summary || {};
    const counts = summary.severity_counts || {};
    const pills = [];
    pills.push(
      '<span class="status-pill">сценарий <strong>' +
        escapeHtml(data.scenario || "") +
        "</strong></span>"
    );
    const crit = counts.critical || 0;
    const warn = counts.warning || 0;
    const info = counts.info || 0;
    if (crit + warn + info > 0 || summary.total_findings != null) {
      pills.push(
        '<button type="button" class="status-pill pill-btn" data-sev="critical">critical/high <strong>' +
          crit +
          "</strong></button>"
      );
      pills.push(
        '<button type="button" class="status-pill pill-btn" data-sev="warning">warning <strong>' +
          warn +
          "</strong></button>"
      );
      pills.push(
        '<button type="button" class="status-pill pill-btn" data-sev="info">info <strong>' +
          info +
          "</strong></button>"
      );
    }
    if (summary.total_findings != null) {
      pills.push(
        '<span class="status-pill">findings <strong>' +
          summary.total_findings +
          "</strong></span>"
      );
    }
    if (summary.common_findings != null || summary.specific_findings != null) {
      pills.push(
        '<span class="status-pill">общие <strong>' +
          (summary.common_findings || 0) +
          "</strong> · специфичные <strong>" +
          (summary.specific_findings || 0) +
          "</strong></span>"
      );
    }
    if (summary.symptoms && summary.symptoms.length) {
      pills.push(
        '<span class="status-pill">симптомы <strong>' +
          escapeHtml(summary.symptoms.join(", ")) +
          "</strong></span>"
      );
    }
    if (summary.nt_runs_symptoms && summary.nt_runs_symptoms.length) {
      pills.push(
        '<span class="status-pill">симптомы <strong>' +
          escapeHtml(summary.nt_runs_symptoms.join(", ")) +
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
        '<span class="status-pill">wiki <strong>' +
          escapeHtml(data.wiki) +
          "</strong></span>"
      );
    }
    els.statusBar.innerHTML = pills.join("");
    els.statusBar.querySelectorAll(".pill-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const sev = btn.getAttribute("data-sev");
        severityFilter = severityFilter === sev ? null : sev;
        renderFindingsCards(data.findings_ui || [], severityFilter);
      });
    });

    renderFindingsCards(data.findings_ui || [], severityFilter);
    bindCheckFlow(sessionId);

    els.downloadWiki.href = apiBase + "/api/sessions/" + sessionId + "/wiki";
    els.downloadWiki.download = data.wiki || "confluence.wiki";
    els.downloadZip.href = apiBase + "/api/sessions/" + sessionId + "/zip";
  }

  async function runAnalysis() {
    showError("");
    if (isJvmMode()) {
      if (!els.jvmSystemName || !els.jvmSystemName.value) {
        showError("выберите АС для check jvm");
        return;
      }
      if (hasPodChoices() && (!els.jvmPodName || !els.jvmPodName.value)) {
        showError("выберите pod");
        return;
      }
      if (!els.jvmContainerName || !els.jvmContainerName.value) {
        showError("выберите контейнер");
        return;
      }
      const selectedProblems = selectedJvmProblems();
      const metricMeta = jvmMetricMeta();
      if (!hasRequiredJvmMetrics(metricMeta)) {
        showError("заполните обязательные поля: GC p95, Heap used (MiB), Memory usage (%)");
        return;
      }
      const missingByProblem = validateJvmProblemInputs(selectedProblems, metricMeta);
      if (missingByProblem.length) {
        showError(
          "Для выбранных проблем заполните обязательные поля: " +
            missingByProblem.join(", ")
        );
        return;
      }
      els.runBtn.disabled = true;
      els.runSpinner.classList.add("visible");
      els.runHint.textContent = "анализ…";
      const meta = {
        mode: "jvm",
        system_name: els.jvmSystemName.value,
        pod_name: (els.jvmPodName && els.jvmPodName.value) || null,
        container_name: els.jvmContainerName.value,
        selected_problems: selectedProblems,
        threshold_profile: (els.jvmThresholdProfile && els.jvmThresholdProfile.value) || "normal",
        jdk_version: _numberOrNull(els.jvmJdkVersion && els.jvmJdkVersion.value),
        spring_boot_version:
          (els.jvmSpringBootVersion && els.jvmSpringBootVersion.value.trim()) || null,
        confluence_title: (els.confluenceTitle && els.confluenceTitle.value.trim()) || null,
        ...metricMeta,
      };
      const form = new FormData();
      form.append("meta", JSON.stringify(meta));
      jvmFiles.forEach((f) => form.append("jvm_file", f.file, f.file.name));
      try {
        const res = await fetch(apiBase + "/api/analyze", {
          method: "POST",
          body: form,
        });
        const data = await res.json();
        if (!res.ok) {
          showError(data.error || "ошибка анализа jvm");
          els.runHint.textContent = "";
          return;
        }
        showResult(data);
        els.runHint.textContent = "готово";
        showToast("jvm анализ готов");
      } catch (err) {
        showError(String(err.message || err));
        els.runHint.textContent = "";
      } finally {
        els.runSpinner.classList.remove("visible");
        updateModeUi();
      }
      return;
    }
    if (!reports.length) {
      showError("добавьте хотя бы один HTML-отчёт");
      return;
    }
    const adv = isAdvancedMode();
    const symptoms = adv ? selectedSymptoms() : [];
    const scenario = adv ? els.scenario.value : "health";

    els.runBtn.disabled = true;
    els.runSpinner.classList.add("visible");
    els.runHint.textContent = "анализ…";

    let sorted = reports.slice().sort((a, b) => a.order - b.order);
    if (!adv) {
      sorted = sorted.slice(0, 1);
    }
    const meta = {
      scenario: scenario,
      symptoms: symptoms,
      confluence_title: adv
        ? els.confluenceTitle.value.trim() || null
        : null,
      query_hex: adv && symptoms.includes("slow_query")
        ? els.queryHex.value.trim() || null
        : null,
      query_id: adv && symptoms.includes("slow_query")
        ? els.queryId.value.trim() || null
        : null,
      query_text: adv && symptoms.includes("slow_query")
        ? els.queryText.value.trim() || null
        : null,
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
      showToast("анализ готов");
    } catch (err) {
      showError(String(err.message || err));
      els.runHint.textContent = "";
    } finally {
      els.runBtn.disabled = !reports.length;
      els.runSpinner.classList.remove("visible");
    }
  }

  function _numberOrNull(value) {
    if (value == null) return null;
    const txt = String(value).trim();
    if (!txt) return null;
    const n = Number(txt);
    return Number.isFinite(n) ? n : null;
  }

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
  if (els.jvmDropzone && els.jvmFileInput) {
    els.jvmDropzone.addEventListener("click", () => els.jvmFileInput.click());
    els.jvmDropzone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        els.jvmFileInput.click();
      }
    });
    els.jvmFileInput.addEventListener("change", () => {
      addJvmFiles(els.jvmFileInput.files);
      els.jvmFileInput.value = "";
    });
    ["dragenter", "dragover"].forEach((ev) => {
      els.jvmDropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        els.jvmDropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((ev) => {
      els.jvmDropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        els.jvmDropzone.classList.remove("dragover");
      });
    });
    els.jvmDropzone.addEventListener("drop", (e) => {
      addJvmFiles(e.dataTransfer.files);
    });
  }

  els.runBtn.addEventListener("click", runAnalysis);
  document.getElementById("copy-wiki").addEventListener("click", () =>
    copyText(els.wikiText.value, "wiki скопирован")
  );
  document.getElementById("copy-verdict").addEventListener("click", () =>
    copyText(extractVerdictAndActions(els.wikiText.value), "вердикт скопирован")
  );
  document.getElementById("copy-prompt").addEventListener("click", () =>
    copyText(els.promptText.value, "промпт скопирован")
  );
  document.querySelectorAll(".wiki-mode").forEach((btn) => {
    btn.addEventListener("click", () => setWikiMode(btn.getAttribute("data-mode")));
  });
  els.scenario.addEventListener("change", updateScenarioHints);
  if (els.modeToggleButtons) {
    els.modeToggleButtons.forEach((btn) => {
      btn.addEventListener("click", () => {
        currentMode = btn.getAttribute("data-mode") || "pg_profile";
        if (!isJvmMode()) {
          jvmFiles = [];
        }
        updateModeUi();
        updateScenarioHints();
      });
    });
  }
  if (els.jvmSystemName) {
    els.jvmSystemName.addEventListener("change", async () => {
      await loadJvmContainers();
      await loadJvmLastInput();
      updateModeUi();
    });
  }
  if (els.jvmPodName) {
    els.jvmPodName.addEventListener("change", async () => {
      renderJvmPodAndContainerSelectors();
      await loadJvmLastInput();
      updateModeUi();
    });
  }
  if (els.jvmContainerName) {
    els.jvmContainerName.addEventListener("change", async () => {
      await loadJvmLastInput();
      updateModeUi();
    });
  }
  if (els.jvmProblemList) {
    els.jvmProblemList.addEventListener("change", updateModeUi);
  }
  [
    els.jvmGcP95,
    els.jvmGcP99,
    els.jvmGcRatio,
    els.jvmMemoryUsagePercent,
    els.jvmHeapUsed,
    els.jvmHeapUsedPercent,
    els.jvmOldgenUsed,
    els.jvmOldgenCapacity,
    els.jvmOldgenUsedPercent,
    els.jvmNewgenUsedMib,
    els.jvmNewgenCapacityMib,
    els.jvmNewgenUsedPercent,
  ].forEach((input) => {
    if (input) input.addEventListener("input", updateModeUi);
  });
  if (els.jvmFillLastValuesBtn) {
    els.jvmFillLastValuesBtn.addEventListener("click", () => {
      if (!jvmLastInput) return;
      applyLastJvmValues(jvmLastInput);
      showToast("подставлены последние значения");
      updateModeUi();
    });
  }
  if (els.advancedSettings) {
    els.advancedSettings.addEventListener("toggle", () => {
      renderReports();
    });
  }

  setTabs();
  loadSymptoms();
  loadJvmSystems();
  loadJvmProblems();
  loadJvmLastInput();
  updateScenarioHints();
})();
