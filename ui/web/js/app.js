/* global API_BASE, WikiPreview */
(function () {
  "use strict";

  const apiBase = typeof API_BASE === "string" ? API_BASE : "";
  let jvmPlaybook = { shoulders: 2, ha_cpu_sum_pct_limit: 80, gc_pause_p95_ms: 250 };

  /** @type {{ id: string, file: File, env: string, label: string, order: number }[]} */
  let reports = [];
  /** @type {{ id: string, file: File }[]} */
  let jvmFiles = [];
  /** @type {{ pod_name: string, container_name: string, display_name: string, java_tool_options_count?: string, java_tool_options_preview?: string }[]} */
  let jvmTargets = [];
  let jvmLastInput = null;
  let currentMode = "pg_profile";
  let sessionId = null;
  let lastWikiText = "";
  let lastCompareSummary = null;
  let severityFilter = null;
  let llmPollTimer = null;
  const compareFilterState = {
    impact: "all",
    confidence: "all",
    evidence_type: "all",
    sort: "delta_desc",
  };
  const CONF_RANK = { high: 3, medium: 2, low: 1 };
  let llmCatalogLoaded = false;
  const LLM_STATUS_RU = {
    idle: "ожидание",
    queued: "в очереди",
    running: "выполняется",
    success: "готово",
    fail: "ошибка",
    blocked: "нельзя публиковать",
  };

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
    jvmWizardSteps: document.getElementById("jvm-wizard-steps"),
    jvmSystemName: document.getElementById("jvm-system-name"),
    jvmNewSystemPanel: document.getElementById("jvm-new-system-panel"),
    jvmNewSystemName: document.getElementById("jvm-new-system-name"),
    jvmNewDropzone: document.getElementById("jvm-new-dropzone"),
    jvmNewFileInput: document.getElementById("jvm-new-file-input"),
    jvmNewFilesList: document.getElementById("jvm-new-files-list"),
    jvmCreateSystemBtn: document.getElementById("jvm-create-system-btn"),
    jvmPodName: document.getElementById("jvm-pod-name"),
    jvmContainerName: document.getElementById("jvm-container-name"),
    jvmPodsPerShoulder: document.getElementById("jvm-pods-per-shoulder"),
    jvmRestartKind: document.getElementById("jvm-restart-kind"),
    jvmHeapGrowing: document.getElementById("jvm-heap-growing"),
    jvmMemoryCauseClosed: document.getElementById("jvm-memory-cause-closed"),
    jvmTreeMemoryClosed: document.getElementById("jvm-tree-memory-closed"),
    jvmTreeHeapMetrics: document.getElementById("jvm-tree-heap-metrics"),
    jvmTreeGcCutoff: document.getElementById("jvm-tree-gc-cutoff"),
    jvmCpuThrottled: document.getElementById("jvm-cpu-throttled"),
    jvmTreeCpuPct: document.getElementById("jvm-tree-cpu-pct"),
    jvmCpuPctShoulder1: document.getElementById("jvm-cpu-pct-shoulder-1"),
    jvmCpuPctShoulder2: document.getElementById("jvm-cpu-pct-shoulder-2"),
    jvmTreeUserLatency: document.getElementById("jvm-tree-user-latency"),
    jvmUserLatencyGrew: document.getElementById("jvm-user-latency-grew"),
    jvmUserLatencyP95: document.getElementById("jvm-user-latency-p95"),
    jvmTreeCoincide: document.getElementById("jvm-tree-coincide"),
    jvmPausesCoincide: document.getElementById("jvm-pauses-coincide"),
    jvmTreeGcCpuSpike: document.getElementById("jvm-tree-gc-cpu-spike"),
    jvmGcCpuSpike: document.getElementById("jvm-gc-cpu-spike"),
    jvmPostGcFloor: document.getElementById("jvm-post-gc-floor"),
    jvmThresholdProfile: document.getElementById("jvm-threshold-profile"),
    jvmJdkVersion: document.getElementById("jvm-jdk-version"),
    jvmSpringBootVersion: document.getElementById("jvm-spring-boot-version"),
    jvmGcP95: document.getElementById("jvm-gc-p95"),
    jvmGcP99: document.getElementById("jvm-gc-p99"),
    jvmGcRatio: document.getElementById("jvm-gc-ratio"),
    jvmMemoryUsagePercent: document.getElementById("jvm-memory-usage-percent"),
    jvmHeapUsed: document.getElementById("jvm-heap-used"),
    jvmHeapUsedPercent: document.getElementById("jvm-heap-used-percent"),
    jvmOldgenMode: document.getElementById("jvm-oldgen-mode"),
    jvmOldgenPercentWrap: document.getElementById("jvm-oldgen-percent-wrap"),
    jvmOldgenMibWrap: document.getElementById("jvm-oldgen-mib-wrap"),
    jvmOldgenCapacityWrap: document.getElementById("jvm-oldgen-capacity-wrap"),
    jvmOldgenUsedMib: document.getElementById("jvm-oldgen-used-mib"),
    jvmOldgenCapacity: document.getElementById("jvm-oldgen-capacity"),
    jvmOldgenUsedPercent: document.getElementById("jvm-oldgen-used-percent"),
    jvmGrowthOf: document.getElementById("jvm-growth-of"),
    jvmHeapGrowthPercent: document.getElementById("jvm-heap-growth-percent"),
    jvmHeapGrowthHours: document.getElementById("jvm-heap-growth-hours"),
    jvmGcRan: document.getElementById("jvm-gc-ran"),
    jvmTreeGcWindow: document.getElementById("jvm-tree-gc-window"),
    jvmHeapBeforeGc: document.getElementById("jvm-heap-before-gc"),
    jvmHeapAfterGc: document.getElementById("jvm-heap-after-gc"),
    jvmOldgenReturned: document.getElementById("jvm-oldgen-returned"),
    jvmNewgenUsedMib: document.getElementById("jvm-newgen-used-mib"),
    jvmNewgenCapacityMib: document.getElementById("jvm-newgen-capacity-mib"),
    jvmNewgenUsedPercent: document.getElementById("jvm-newgen-used-percent"),
    jvmFillLastValuesBtn: document.getElementById("jvm-fill-last-values-btn"),
    jvmHistoryHint: document.getElementById("jvm-history-hint"),
    jvmReviewPanel: document.getElementById("jvm-review-panel"),
    jvmReviewText: document.getElementById("jvm-review-text"),
    jvmReviewConfirm: document.getElementById("jvm-review-confirm"),
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
    compareInsights: document.getElementById("compare-insights"),
    findingsCards: document.getElementById("findings-cards"),
    checkFlow: document.getElementById("check-flow"),
    wikiText: document.getElementById("wiki-text"),
    wikiPreview: document.getElementById("wiki-preview"),
    promptText: document.getElementById("prompt-text"),
    briefText: document.getElementById("brief-text"),
    qualityText: document.getElementById("quality-text"),
    downloadWiki: document.getElementById("download-wiki"),
    downloadZip: document.getElementById("download-zip"),
    confluenceTitle: document.getElementById("confluence-title"),
    queryHex: document.getElementById("query-hex"),
    queryId: document.getElementById("query-id"),
    queryText: document.getElementById("query-text"),
    llmPanel: document.getElementById("llm-panel"),
    llmTask: document.getElementById("llm-task"),
    llmProvider: document.getElementById("llm-provider"),
    llmProviderHint: document.getElementById("llm-provider-hint"),
    llmExtra: document.getElementById("llm-extra"),
    llmRunBtn: document.getElementById("llm-run-btn"),
    llmSpinner: document.getElementById("llm-spinner"),
    llmStatus: document.getElementById("llm-status"),
    llmPolicy: document.getElementById("llm-policy"),
    llmQuality: document.getElementById("llm-quality"),
    llmMeta: document.getElementById("llm-meta"),
    llmAnswer: document.getElementById("llm-answer"),
    llmAnswerActions: document.getElementById("llm-answer-actions"),
    llmPublishNote: document.getElementById("llm-publish-note"),
    copyLlmAnswer: document.getElementById("copy-llm-answer"),
  };
  let llmUiAvailable = false;

  function isAdvancedMode() {
    return !!(els.advancedSettings && els.advancedSettings.open);
  }

  function isJvmMode() {
    return currentMode === "jvm";
  }

  function jvmTreeAnswers() {
    return {
      pods_per_shoulder: _numberOrNull(els.jvmPodsPerShoulder && els.jvmPodsPerShoulder.value),
      restart_kind: (els.jvmRestartKind && els.jvmRestartKind.value) || "none",
      heap_growing: (els.jvmHeapGrowing && els.jvmHeapGrowing.value) || "no",
      heap_growth_percent: _numberOrNull(els.jvmHeapGrowthPercent && els.jvmHeapGrowthPercent.value),
      heap_growth_hours: _numberOrNull(els.jvmHeapGrowthHours && els.jvmHeapGrowthHours.value),
      growth_of: (els.jvmGrowthOf && els.jvmGrowthOf.value) || "unknown",
      gc_ran_in_window: (els.jvmGcRan && els.jvmGcRan.value) || "unknown",
      heap_used_before_gc_mib:
        (els.jvmGcRan && els.jvmGcRan.value) === "yes"
          ? _intOrNull(els.jvmHeapBeforeGc && els.jvmHeapBeforeGc.value)
          : null,
      heap_used_after_gc_mib:
        (els.jvmGcRan && els.jvmGcRan.value) === "yes"
          ? _intOrNull(els.jvmHeapAfterGc && els.jvmHeapAfterGc.value)
          : null,
      oldgen_returned_after_gc:
        (els.jvmGcRan && els.jvmGcRan.value) === "yes"
          ? (els.jvmOldgenReturned && els.jvmOldgenReturned.value) || "unknown"
          : "unknown",
      memory_cause_closed: (els.jvmMemoryCauseClosed && els.jvmMemoryCauseClosed.value) || "unknown",
      cpu_throttled: (els.jvmCpuThrottled && els.jvmCpuThrottled.value) || "no",
      cpu_pct_limits_shoulder_1: _numberOrNull(els.jvmCpuPctShoulder1 && els.jvmCpuPctShoulder1.value),
      cpu_pct_limits_shoulder_2: _numberOrNull(els.jvmCpuPctShoulder2 && els.jvmCpuPctShoulder2.value),
      user_latency_grew: (els.jvmUserLatencyGrew && els.jvmUserLatencyGrew.value) || "unknown",
      user_latency_p95_ms: _numberOrNull(els.jvmUserLatencyP95 && els.jvmUserLatencyP95.value),
      pauses_coincide_throttle: (els.jvmPausesCoincide && els.jvmPausesCoincide.value) || "unknown",
      post_gc_floor_rising:
        (els.jvmGcRan && els.jvmGcRan.value) === "yes"
          ? (els.jvmPostGcFloor && els.jvmPostGcFloor.value) || "unknown"
          : "unknown",
      gc_cpu_spike_sla: (els.jvmGcCpuSpike && els.jvmGcCpuSpike.value) || "unknown",
    };
  }

  function jvmOldgenMode() {
    return (els.jvmOldgenMode && els.jvmOldgenMode.value) || "percent";
  }

  function jvmMetricMeta() {
    const mode = jvmOldgenMode();
    let oldPct = null;
    let oldMib = null;
    let oldCap = null;
    if (mode === "mib") {
      oldMib = _intOrNull(els.jvmOldgenUsedMib && els.jvmOldgenUsedMib.value);
      oldCap = _intOrNull(els.jvmOldgenCapacity && els.jvmOldgenCapacity.value);
      if (oldMib != null && oldCap) oldPct = (100 * oldMib) / oldCap;
    } else {
      oldPct = _numberOrNull(els.jvmOldgenUsedPercent && els.jvmOldgenUsedPercent.value);
    }
    return {
      gc_pause_p95_ms: _numberOrNull(els.jvmGcP95 && els.jvmGcP95.value),
      heap_used_mib: _intOrNull(els.jvmHeapUsed && els.jvmHeapUsed.value),
      old_gen_used_percent: oldPct,
      old_gen_used_mib: oldMib,
      old_gen_capacity_mib: oldCap,
      container_memory_usage_percent: _numberOrNull(
        els.jvmMemoryUsagePercent && els.jvmMemoryUsagePercent.value
      ),
    };
  }

  function hasAnyJvmMetric(meta) {
    return Object.values(meta || {}).some((v) => typeof v === "number");
  }

  function selectedJvmProblems() {
    return [];
  }

  function hasPodChoices() {
    return jvmTargets.some((target) => !!(target.pod_name || "").trim());
  }

  function getSelectedTarget() {
    const pod = (els.jvmPodName && els.jvmPodName.value) || "";
    const container = (els.jvmContainerName && els.jvmContainerName.value) || "";
    return jvmTargets.find(
      (target) => target.container_name === container && (target.pod_name || "") === pod
    );
  }

  function renderJvmMetricVisibility() {
    const tree = jvmTreeAnswers();
    const p95 = _numberOrNull(els.jvmGcP95 && els.jvmGcP95.value);
    const p95Limit = Number(jvmPlaybook.gc_pause_p95_ms || 250);
    const heapOpen =
      tree.restart_kind === "oomkilled" ||
      tree.restart_kind === "java_oome" ||
      tree.heap_growing === "yes";
    const gcOpen = p95 != null && p95 > p95Limit;
    if (els.jvmTreeMemoryClosed) {
      els.jvmTreeMemoryClosed.hidden = !(
        tree.restart_kind === "oomkilled" || tree.restart_kind === "java_oome"
      );
    }
    if (els.jvmTreeHeapMetrics) els.jvmTreeHeapMetrics.hidden = !heapOpen;
    if (els.jvmOldgenPercentWrap) els.jvmOldgenPercentWrap.hidden = jvmOldgenMode() === "mib";
    if (els.jvmOldgenMibWrap) els.jvmOldgenMibWrap.hidden = jvmOldgenMode() !== "mib";
    if (els.jvmOldgenCapacityWrap) els.jvmOldgenCapacityWrap.hidden = jvmOldgenMode() !== "mib";
    if (els.jvmTreeGcWindow) {
      els.jvmTreeGcWindow.hidden = !(heapOpen && tree.gc_ran_in_window === "yes");
    }
    if (els.jvmTreeGcCutoff) els.jvmTreeGcCutoff.hidden = gcOpen;
    if (els.jvmTreeCpuPct) els.jvmTreeCpuPct.hidden = tree.cpu_throttled !== "yes";
    if (els.jvmTreeUserLatency) els.jvmTreeUserLatency.hidden = !gcOpen;
    if (els.jvmTreeCoincide) {
      els.jvmTreeCoincide.hidden = !(tree.cpu_throttled === "yes" && gcOpen);
    }
    if (els.jvmTreeGcCpuSpike) {
      els.jvmTreeGcCpuSpike.hidden = !(gcOpen || (heapOpen && tree.gc_ran_in_window === "yes"));
    }
  }

  function validateJvmInline() {
    return {};
  }

  function renderJvmReview() {
    if (!els.jvmReviewText) return;
    const target = getSelectedTarget();
    const tree = jvmTreeAnswers();
    const metrics = jvmMetricMeta();
    const lines = [
      "Система: " + (selectedJvmSystemName() || (isJvmNewSystem() ? "новая АС" : "—")),
      "Pod: " + (((els.jvmPodName && els.jvmPodName.value) || "—")),
      "Контейнер: " + ((els.jvmContainerName && els.jvmContainerName.value) || "—"),
      "Подов на плече: " + (tree.pods_per_shoulder != null ? tree.pods_per_shoulder : "—") + " (плеч 2)",
      "Рестарт: " + tree.restart_kind,
      "Память растёт: " + tree.heap_growing,
      tree.heap_growing === "yes"
        ? "Рост: " +
          (tree.heap_growth_percent != null ? tree.heap_growth_percent + "%" : "—") +
          " за " +
          (tree.heap_growth_hours != null ? tree.heap_growth_hours + " ч" : "—")
        : null,
      "CPU throttle: " + tree.cpu_throttled,
      "GC p95: " + (metrics.gc_pause_p95_ms != null ? metrics.gc_pause_p95_ms : "не знаю"),
      target
        ? "JVM flags: " +
          Number(target.java_tool_options_count || 0) +
          ((target.java_tool_options_preview || "").trim()
            ? " (preview: " + target.java_tool_options_preview + ")"
            : "")
        : "JVM flags: —",
    ];
    els.jvmReviewText.innerHTML = lines
      .filter(Boolean)
      .map((line) => "<div>" + escapeHtml(line) + "</div>")
      .join("");
  }

  function renderJvmWizardSteps() {
    if (!els.jvmWizardSteps) return;
    const step1 = !!selectedJvmSystemName();
    const step2 = !hasPodChoices() || !!(els.jvmPodName && els.jvmPodName.value);
    const step3 = !!(els.jvmContainerName && els.jvmContainerName.value);
    const step4 = true;
    const items = [
      ["1) Система", step1],
      ["2) Pod", step2],
      ["3) Контейнер", step3],
      ["4) Дерево", step4],
    ];
    els.jvmWizardSteps.innerHTML = items
      .map(([label, ok]) => {
        return (
          '<span class="status-pill">' +
          escapeHtml(label) +
          " <strong>" +
          (ok ? "OK" : "TODO") +
          "</strong></span>"
        );
      })
      .join("");
  }

  function _setJvmMetricValue(input, value) {
    if (!input) return;
    input.value = value == null ? "" : String(value);
  }

  function applyLastJvmValues(values) {
    if (!values) return;
    _setJvmMetricValue(els.jvmGcP95, values.gc_pause_p95_ms);
    _setJvmMetricValue(els.jvmHeapUsed, values.heap_used_mib);
    _setJvmMetricValue(els.jvmOldgenUsedPercent, values.old_gen_used_percent);
    _setJvmMetricValue(els.jvmOldgenUsedMib, values.old_gen_used_mib);
    _setJvmMetricValue(els.jvmOldgenCapacity, values.old_gen_capacity_mib);
    if (els.jvmOldgenMode) {
      els.jvmOldgenMode.value =
        values.old_gen_used_mib != null && values.old_gen_used_percent == null ? "mib" : "percent";
    }
    _setJvmMetricValue(els.jvmMemoryUsagePercent, values.container_memory_usage_percent);
    const tree = values.tree || {};
    _setJvmMetricValue(els.jvmPodsPerShoulder, tree.pods_per_shoulder);
    if (els.jvmRestartKind && tree.restart_kind) els.jvmRestartKind.value = tree.restart_kind;
    if (els.jvmHeapGrowing && tree.heap_growing) els.jvmHeapGrowing.value = tree.heap_growing;
    _setJvmMetricValue(els.jvmHeapGrowthPercent, tree.heap_growth_percent);
    _setJvmMetricValue(els.jvmHeapGrowthHours, tree.heap_growth_hours);
    if (els.jvmGrowthOf && tree.growth_of) els.jvmGrowthOf.value = tree.growth_of;
    if (els.jvmGcRan && tree.gc_ran_in_window) els.jvmGcRan.value = tree.gc_ran_in_window;
    _setJvmMetricValue(els.jvmHeapBeforeGc, tree.heap_used_before_gc_mib);
    _setJvmMetricValue(els.jvmHeapAfterGc, tree.heap_used_after_gc_mib);
    if (els.jvmOldgenReturned && tree.oldgen_returned_after_gc) {
      els.jvmOldgenReturned.value = tree.oldgen_returned_after_gc;
    }
    if (els.jvmMemoryCauseClosed && tree.memory_cause_closed) {
      els.jvmMemoryCauseClosed.value = tree.memory_cause_closed;
    }
    if (els.jvmCpuThrottled && tree.cpu_throttled) els.jvmCpuThrottled.value = tree.cpu_throttled;
    _setJvmMetricValue(els.jvmCpuPctShoulder1, tree.cpu_pct_limits_shoulder_1);
    _setJvmMetricValue(els.jvmCpuPctShoulder2, tree.cpu_pct_limits_shoulder_2);
    if (els.jvmUserLatencyGrew && tree.user_latency_grew) {
      els.jvmUserLatencyGrew.value = tree.user_latency_grew;
    }
    _setJvmMetricValue(els.jvmUserLatencyP95, tree.user_latency_p95_ms);
    if (els.jvmPausesCoincide && tree.pauses_coincide_throttle) {
      els.jvmPausesCoincide.value = tree.pauses_coincide_throttle;
    }
    if (els.jvmPostGcFloor && tree.post_gc_floor_rising) {
      els.jvmPostGcFloor.value = tree.post_gc_floor_rising;
    }
    if (els.jvmGcCpuSpike && tree.gc_cpu_spike_sla) {
      els.jvmGcCpuSpike.value = tree.gc_cpu_spike_sla;
    }
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
          "// дерево отсечения · плечи k8s · ворота JAVA_TOOL_OPTIONS";
      }
      if (els.simpleAnalysisHint) {
        els.simpleAnalysisHint.textContent =
          "Последовательное дерево: рестарты, CPU throttle по двум плечам, затем GC p95. Копируемую строку флагов даём только если узлы закрыты.";
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
      renderJvmMetricVisibility();
      renderJvmReview();
      renderJvmWizardSteps();
      syncJvmNewSystemUi();
      if (els.runBtn) {
        const ready = !!(
          selectedJvmSystemName() &&
          (!hasPodChoices() || (els.jvmPodName && els.jvmPodName.value)) &&
          els.jvmContainerName &&
          els.jvmContainerName.value
        );
        els.runBtn.disabled = !ready;
      }
      if (els.jvmFillLastValuesBtn) {
        els.jvmFillLastValuesBtn.disabled = !jvmLastInput;
      }
      if (els.jvmHistoryHint) {
        const hasSystem = !!selectedJvmSystemName();
        const hasContainer = !!(els.jvmContainerName && els.jvmContainerName.value);
        if (isJvmNewSystem()) {
          els.jvmHistoryHint.textContent = "сначала подгрузите файлы новой АС";
        } else if (!hasSystem || !hasContainer) {
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
        if (isJvmNewSystem()) {
          els.runHint.textContent = "подгрузите файлы новой АС";
        } else if (!selectedJvmSystemName()) {
          els.runHint.textContent = "выберите АС";
        } else if (hasPodChoices() && (!els.jvmPodName || !els.jvmPodName.value)) {
          els.runHint.textContent = "выберите pod";
        } else if (!els.jvmContainerName || !els.jvmContainerName.value) {
          els.runHint.textContent = "выберите контейнер";
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

  const JVM_NEW_SYSTEM = "__new__";

  function isJvmNewSystem() {
    return !!(els.jvmSystemName && els.jvmSystemName.value === JVM_NEW_SYSTEM);
  }

  function selectedJvmSystemName() {
    if (isJvmNewSystem()) return "";
    return (els.jvmSystemName && els.jvmSystemName.value) || "";
  }

  function syncJvmNewSystemUi() {
    const isNew = isJvmNewSystem();
    if (els.jvmNewSystemPanel) els.jvmNewSystemPanel.hidden = !isNew;
    const filesNote = jvmFiles.length
      ? "Загружены файлы: " + jvmFiles.map((f) => f.file.name).join(", ")
      : "Файлы ещё не выбраны.";
    if (els.jvmNewFilesList) els.jvmNewFilesList.textContent = filesNote;
    if (els.jvmCreateSystemBtn) {
      const name = (els.jvmNewSystemName && els.jvmNewSystemName.value.trim()) || "";
      els.jvmCreateSystemBtn.disabled = !(isNew && name && jvmFiles.length);
    }
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

  async function loadJvmSystems(selectName) {
    if (!els.jvmSystemName) return;
    try {
      const res = await fetch(apiBase + "/api/jvm/systems");
      const data = await res.json();
      const systems = data.systems || [];
      const previous = selectName || els.jvmSystemName.value || "";
      els.jvmSystemName.innerHTML = "";
      const first = document.createElement("option");
      first.value = "";
      first.textContent = systems.length ? "выберите систему" : "системы не найдены";
      els.jvmSystemName.appendChild(first);
      systems.forEach((name) => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name === "__root__" ? "(resources root)" : name;
        els.jvmSystemName.appendChild(opt);
      });
      const addNew = document.createElement("option");
      addNew.value = JVM_NEW_SYSTEM;
      addNew.textContent = "добавить новую систему";
      els.jvmSystemName.appendChild(addNew);
      if (previous && (previous === JVM_NEW_SYSTEM || systems.includes(previous))) {
        els.jvmSystemName.value = previous;
      }
      await loadJvmContainers();
      updateModeUi();
    } catch (err) {
      els.jvmSystemName.innerHTML = '<option value="">ошибка загрузки систем</option>';
      updateModeUi();
    }
  }

  async function createJvmSystemFromUi() {
    const name = (els.jvmNewSystemName && els.jvmNewSystemName.value.trim()) || "";
    if (!name) {
      showError("укажите имя новой АС");
      return;
    }
    if (!jvmFiles.length) {
      showError("перетащите resources.yaml (и желательно jvm-config.txt)");
      return;
    }
    if (els.jvmCreateSystemBtn) els.jvmCreateSystemBtn.disabled = true;
    const form = new FormData();
    form.append("system_name", name);
    jvmFiles.forEach((f) => form.append("jvm_file", f.file, f.file.name));
    try {
      const res = await fetch(apiBase + "/api/jvm/systems", {
        method: "POST",
        body: form,
      });
      const data = await res.json();
      if (!res.ok) {
        showError(data.error || "не удалось создать АС");
        return;
      }
      jvmFiles = [];
      if (els.jvmNewSystemName) els.jvmNewSystemName.value = "";
      showError("");
      await loadJvmSystems(data.system);
      await loadJvmLastInput();
    } catch (err) {
      showError("не удалось подгрузить файлы новой АС");
    } finally {
      updateModeUi();
    }
  }

  async function loadJvmPlaybook() {
    try {
      const res = await fetch(apiBase + "/api/jvm/playbook");
      const data = await res.json();
      if (data.playbook) jvmPlaybook = data.playbook;
    } catch (_) {
      jvmPlaybook = { shoulders: 2, ha_cpu_sum_pct_limit: 80, gc_pause_p95_ms: 250 };
    }
  }

  async function loadJvmContainers() {
    if (!els.jvmContainerName || !els.jvmSystemName) return;
    const systemName = selectedJvmSystemName();
    if (!systemName) {
      if (els.jvmPodName) {
        els.jvmPodName.innerHTML = isJvmNewSystem()
          ? '<option value="">сначала подгрузите файлы новой АС</option>'
          : '<option value="">сначала выберите АС</option>';
      }
      els.jvmContainerName.innerHTML = isJvmNewSystem()
        ? '<option value="">сначала подгрузите файлы новой АС</option>'
        : '<option value="">сначала выберите АС</option>';
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
          return {
            pod_name: "",
            container_name: item,
            display_name: item,
            java_tool_options_count: "0",
            java_tool_options_preview: "",
          };
        }
        return {
          pod_name: item.pod_name || "",
          container_name: item.container_name || "",
          display_name:
            item.display_name ||
            ((item.pod_name ? item.pod_name + " / " : "") + (item.container_name || "")),
          java_tool_options_count: item.java_tool_options_count || "0",
          java_tool_options_preview: item.java_tool_options_preview || "",
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
      } else if (pods.includes("app")) {
        els.jvmPodName.value = "app";
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
    } else if (names.includes("application")) {
      els.jvmContainerName.value = "application";
    }
  }

  async function loadJvmLastInput() {
    jvmLastInput = null;
    const hasSystem = !!selectedJvmSystemName();
    const hasPod = !hasPodChoices() || !!(els.jvmPodName && els.jvmPodName.value);
    const hasContainer = !!(els.jvmContainerName && els.jvmContainerName.value);
    if (!hasSystem || !hasPod || !hasContainer) {
      updateModeUi();
      return;
    }
    try {
      const res = await fetch(
        apiBase +
          "/api/jvm/last-input?system=" +
          encodeURIComponent(selectedJvmSystemName()) +
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

  function applyQwenAvailability(available) {
    llmUiAvailable = Boolean(available);
    document.body.classList.toggle("qwen-unavailable", !llmUiAvailable);
    document.body.classList.toggle("qwen-available", llmUiAvailable);
    if (!llmUiAvailable && els.llmPanel) {
      els.llmPanel.hidden = true;
    }
    const qualityBtn = document.querySelector('.tab-btn[data-tab="quality"]');
    if (qualityBtn && qualityBtn.classList.contains("active") && !llmUiAvailable) {
      activateTab("wiki");
    }
  }

  async function loadQwenStatus() {
    try {
      const res = await fetch(apiBase + "/api/llm/status");
      const data = res.ok ? await res.json() : {};
      applyQwenAvailability(Boolean(data.available));
    } catch (_) {
      applyQwenAvailability(false);
    }
  }

  function activateTab(tab) {
    if (tab === "quality" && !llmUiAvailable) return;
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tab") === tab);
    });
    document.querySelectorAll(".tab-panel").forEach((p) => {
      p.classList.toggle("active", p.id === "tab-" + tab);
    });
  }

  function setTabs() {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => {
        const tab = btn.getAttribute("data-tab");
        if (tab) activateTab(tab);
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

  function workloadIsWeak(workload) {
    const level = String((workload && workload.level) || "").toLowerCase();
    const score = workload && workload.workload_match_score;
    if (level === "low") return true;
    return typeof score === "number" && Number.isFinite(score) && score < 0.6;
  }

  function collectChangedParams(influence, rows) {
    const fromSettings = ((influence.settings_table || {}).rows || [])
      .map(function (row) {
        return String(row.parameter || "").trim();
      })
      .filter(Boolean);
    if (fromSettings.length) return fromSettings;
    const seen = [];
    rows.forEach(function (row) {
      const name = String(row.parameter || "").trim();
      if (name && seen.indexOf(name) === -1) seen.push(name);
    });
    return seen;
  }

  function collectConfidenceHints(summary, rows, confidence) {
    const seen = {};
    const out = [];
    function add(text) {
      const t = String(text || "").trim();
      if (!t || seen[t] || out.length >= 2) return;
      seen[t] = true;
      out.push(t);
    }
    const trail = ((summary && summary.quality) || {}).confidence_trail || [];
    trail.forEach(function (item) {
      if (item && item.change === "downgrade") add(item.reason);
    });
    rows.forEach(function (row) {
      (row.confidence_reasons || []).forEach(add);
    });
    add(confidence && confidence.notes);
    return out;
  }

  function filterAndSortInfluenceRows(rows, state) {
    const filtered = rows.filter(function (row) {
      if (
        state.impact !== "all" &&
        String(row.impact || "neutral").toLowerCase() !== state.impact
      ) {
        return false;
      }
      if (
        state.confidence !== "all" &&
        String(row.confidence || "").toLowerCase() !== state.confidence
      ) {
        return false;
      }
      if (
        state.evidence_type !== "all" &&
        String(row.evidence_type || "").toLowerCase() !== state.evidence_type
      ) {
        return false;
      }
      return true;
    });
    const sorted = filtered.slice();
    sorted.sort(function (a, b) {
      if (state.sort === "confidence") {
        const ra = CONF_RANK[String(a.confidence || "").toLowerCase()] || 0;
        const rb = CONF_RANK[String(b.confidence || "").toLowerCase()] || 0;
        return rb - ra;
      }
      const da = Math.abs(typeof a.delta_pct === "number" ? a.delta_pct : 0);
      const db = Math.abs(typeof b.delta_pct === "number" ? b.delta_pct : 0);
      return state.sort === "delta_asc" ? da - db : db - da;
    });
    return sorted;
  }

  function compareSelect(name, selected, options) {
    const opts = options
      .map(function (item) {
        return (
          '<option value="' +
          escapeHtml(item[0]) +
          '"' +
          (selected === item[0] ? " selected" : "") +
          ">" +
          escapeHtml(item[1]) +
          "</option>"
        );
      })
      .join("");
    return (
      '<label class="compare-filter">' +
      escapeHtml(name) +
      ' <select data-compare-filter="' +
      escapeHtml(name) +
      '">' +
      opts +
      "</select></label>"
    );
  }

  function renderCompareInsights(summary) {
    const root = els.compareInsights;
    if (!root) return;
    const influence = (summary && summary.influence) || null;
    if (!influence) {
      root.hidden = true;
      root.innerHTML = "";
      return;
    }
    lastCompareSummary = summary;
    const rows = Array.isArray(influence.rows) ? influence.rows : [];
    const functional = influence.functional_summary || {};
    const workload = influence.workload_match || {};
    const confidence = influence.confidence_meta || {};
    const topRows = filterAndSortInfluenceRows(rows, compareFilterState).slice(0, 50);
    const improvedCount =
      functional.improved_count != null
        ? functional.improved_count
        : functional.improved_pairs != null
        ? functional.improved_pairs
        : 0;
    const degradedCount =
      functional.degraded_count != null
        ? functional.degraded_count
        : functional.degraded_pairs != null
        ? functional.degraded_pairs
        : 0;
    const settingsTable = influence.settings_table || null;
    const metricsTable = influence.metrics_table || null;
    const compare = (summary && summary.compare) || {};
    const seriesRunLabels = (settingsTable && settingsTable.run_labels) || [];
    const modeLabel =
      compare.mode === "series" || compare.mode === "pair"
        ? compare.mode
        : influence.mode === "series" ||
          influence.type === "influence_table_series" ||
          (influence.mode !== "pair" && seriesRunLabels.length >= 2)
        ? "series"
        : "pair";

    function formatMetricValue(value) {
      if (value == null) return "—";
      if (typeof value === "number") {
        const abs = Math.abs(value);
        if (abs >= 1000) return value.toFixed(0);
        if (abs >= 10) return value.toFixed(2);
        return value.toFixed(3);
      }
      return String(value);
    }

    function formatDeltaCell(deltaObj) {
      if (!deltaObj || typeof deltaObj !== "object") return "—";
      const d = deltaObj.delta;
      const pct = deltaObj.delta_pct;
      if (typeof d !== "number" && typeof pct !== "number") return "—";
      const dTxt =
        typeof d === "number"
          ? (d > 0 ? "+" : "") + formatMetricValue(d)
          : "";
      const pTxt =
        typeof pct === "number"
          ? (pct > 0 ? "+" : "") + pct.toFixed(1) + "%"
          : "";
      if (dTxt && pTxt) return dTxt + " (" + pTxt + ")";
      return dTxt || pTxt || "—";
    }
    const changedCount = Number(confidence.changed_params_count || 0);
    const changedThreshold = Number(confidence.changed_params_threshold || 0);
    const showManyChangesWarning =
      Number.isFinite(changedCount) &&
      Number.isFinite(changedThreshold) &&
      changedCount > changedThreshold;

    const tableRows = topRows
      .map((row) => {
        const impact = String(row.impact || "neutral").toLowerCase();
        const deltaPct =
          typeof row.delta_pct === "number"
            ? (row.delta_pct > 0 ? "+" : "") + row.delta_pct.toFixed(1) + "%"
            : "—";
        const iqrPct =
          typeof row.delta_iqr_pct === "number"
            ? (row.delta_iqr_pct > 0 ? "+" : "") + row.delta_iqr_pct.toFixed(1) + "%"
            : "—";
        const stability =
          typeof row.stability_score === "number"
            ? row.stability_score.toFixed(3)
            : "—";
        const metricDir = String(row.metric_direction || (modeLabel === "series" ? "—" : row.direction) || "—");
        const gucDir = modeLabel === "series" ? String(row.direction || "—") : "—";
        const evidenceCount =
          row.evidence_count == null || row.evidence_count === ""
            ? "—"
            : String(row.evidence_count);
        return (
          '<tr class="impact-' +
          escapeHtml(impact) +
          '">' +
          "<td><code>" +
          escapeHtml(row.parameter || "") +
          "</code></td>" +
          "<td>" +
          escapeHtml(row.old == null ? "—" : String(row.old)) +
          "</td>" +
          "<td>" +
          escapeHtml(row.new == null ? "—" : String(row.new)) +
          "</td>" +
          "<td>" +
          escapeHtml(gucDir) +
          "</td>" +
          "<td>" +
          escapeHtml(row.affected_metric || "—") +
          "</td>" +
          "<td>" +
          escapeHtml(metricDir) +
          "</td>" +
          "<td>" +
          escapeHtml(deltaPct) +
          "</td>" +
          "<td>" +
          escapeHtml(iqrPct) +
          "</td>" +
          "<td>" +
          escapeHtml(impact) +
          "</td>" +
          "<td>" +
          escapeHtml(stability) +
          "</td>" +
          "<td>" +
          escapeHtml(String(row.confidence || "—")) +
          "</td>" +
          "<td>" +
          escapeHtml(evidenceCount) +
          "</td>" +
          "</tr>"
        );
      })
      .join("");

    let extraSeriesHtml = "";
    if (modeLabel === "series" && settingsTable && metricsTable) {
      const runLabels = settingsTable.run_labels || [];
      const settingsRows = (settingsTable.rows || [])
        .map((row) => {
          const cells = runLabels
            .map((label) => "<td>" + escapeHtml(String((row.values || {})[label] || "—")) + "</td>")
            .join("");
          return (
            "<tr><td><code>" +
            escapeHtml(row.parameter || "") +
            "</code></td>" +
            cells +
            "</tr>"
          );
        })
        .join("");
      const settingsHead =
        "<tr><th>Параметр</th>" +
        runLabels.map((label) => "<th>" + escapeHtml(label) + "</th>").join("") +
        "</tr>";

      const metricRunLabels = metricsTable.run_labels || [];
      const pairLabels = metricsTable.pair_labels || [];
      const metricsRows = (metricsTable.rows || [])
        .slice(0, 80)
        .map((row) => {
          let cells = "<td><code>" + escapeHtml(row.metric || "") + "</code></td>";
          metricRunLabels.forEach((label, idx) => {
            cells += "<td>" + escapeHtml(formatMetricValue((row.values || {})[label])) + "</td>";
            if (idx > 0) {
              const pair = pairLabels[idx - 1];
              cells += "<td>" + escapeHtml(formatDeltaCell((row.deltas || {})[pair])) + "</td>";
            }
          });
          cells += "<td>" + escapeHtml(String(row.trend || "—")) + "</td>";
          return "<tr>" + cells + "</tr>";
        })
        .join("");
      let metricsHead = "<tr><th>Метрика</th>";
      metricRunLabels.forEach((label, idx) => {
        metricsHead += "<th>" + escapeHtml(label) + "</th>";
        if (idx > 0) {
          metricsHead += "<th>Δ " + escapeHtml(metricRunLabels[idx - 1]) + "→" + escapeHtml(label) + "</th>";
        }
      });
      metricsHead += "<th>Тренд</th></tr>";

      extraSeriesHtml =
        '<div class="compare-series-block">' +
        '<h3 class="section-title">Отличающиеся настройки по прогонам</h3>' +
        '<div class="compare-table-wrap"><table class="compare-table"><thead>' +
        settingsHead +
        "</thead><tbody>" +
        settingsRows +
        "</tbody></table></div>" +
        '<h3 class="section-title" style="margin-top:0.8rem;">Изменения результатов по метрикам</h3>' +
        '<div class="compare-table-wrap"><table class="compare-table"><thead>' +
        metricsHead +
        "</thead><tbody>" +
        metricsRows +
        "</tbody></table></div>" +
        "</div>";
    }

    const genericInfluenceTableHtml =
      '<h3 class="section-title" style="margin-top:0.8rem;">Связи параметр → метрика</h3>' +
      '<div class="compare-filters">' +
      compareSelect("impact", compareFilterState.impact, [
        ["all", "все"],
        ["improved", "improved"],
        ["degraded", "degraded"],
        ["neutral", "neutral"],
      ]) +
      compareSelect("confidence", compareFilterState.confidence, [
        ["all", "все"],
        ["high", "high"],
        ["medium", "medium"],
        ["low", "low"],
      ]) +
      compareSelect("evidence_type", compareFilterState.evidence_type, [
        ["all", "все"],
        ["probable", "probable"],
        ["proven", "proven"],
      ]) +
      compareSelect("sort", compareFilterState.sort, [
        ["delta_desc", "|Δ%| ↓"],
        ["delta_asc", "|Δ%| ↑"],
        ["confidence", "confidence ↓"],
      ]) +
      '<span class="compare-filter-count">показано ' +
      escapeHtml(String(topRows.length)) +
      " из " +
      escapeHtml(String(rows.length)) +
      "</span>" +
      "</div>" +
      '<p class="compare-hint">probable — гипотеза; proven — изоляция (пара) или устойчивая серия.</p>' +
      '<div class="compare-table-wrap"><table class="compare-table"><thead><tr>' +
      "<th>parameter</th><th>old</th><th>new</th><th>GUC</th><th>affected_metric</th><th>метрика Δ</th><th>delta% (median)</th><th>IQR%</th><th>impact</th><th>stability</th><th>confidence</th><th>" +
      escapeHtml(modeLabel === "series" ? "Пар" : "Есть связь") +
      "</th>" +
      "</tr></thead><tbody>" +
      tableRows +
      "</tbody></table></div>";

    const oracle = (summary && summary.oracle) || {};
    const oracleReasons = Array.isArray(oracle.reasons) ? oracle.reasons : [];
    const oracleAdjustments = Array.isArray(oracle.confidence_adjustments)
      ? oracle.confidence_adjustments
      : [];
    const oracleLayerBits = (Array.isArray(oracle.layers) ? oracle.layers : [])
      .map(function (layer) {
        if (!layer || !layer.name) return "";
        return (
          layer.name +
          "=" +
          (layer.verdict || "?") +
          (layer.skipped ? " (пропущен)" : "")
        );
      })
      .filter(Boolean);
    let oracleHtml = "";
    if (oracle.verdict && (oracleReasons.length || oracleAdjustments.length)) {
      const reasonHtml = oracleReasons
        .slice(0, 8)
        .map(function (reason) {
          return escapeHtml(String(reason));
        })
        .join("<br>");
      const adjHtml = oracleAdjustments
        .slice(0, 8)
        .map(function (item) {
          return (
            escapeHtml(String(item.parameter || "?")) +
            ": " +
            escapeHtml(String(item.from || "?")) +
            " → " +
            escapeHtml(String(item.to || "?")) +
            (item.reason ? " (" + escapeHtml(String(item.reason)) + ")" : "")
          );
        })
        .join("<br>");
      oracleHtml =
        '<div class="compare-warning oracle-box oracle-' +
        escapeHtml(String(oracle.verdict)) +
        '"><strong>Oracle ' +
        escapeHtml(String(oracle.verdict)) +
        "</strong>" +
        (oracleLayerBits.length
          ? " · " + escapeHtml(oracleLayerBits.join(", "))
          : "") +
        (reasonHtml ? "<br>" + reasonHtml : "") +
        (adjHtml ? "<br><strong>Confidence:</strong><br>" + adjHtml : "") +
        "</div>";
    }

    const changedParams =
      Array.isArray(compare.changed_params) && compare.changed_params.length
        ? compare.changed_params
        : collectChangedParams(influence, rows);
    const shownGucs = changedParams.slice(0, 15);
    const extraGucs = changedParams.length - shownGucs.length;
    const gucHtml = shownGucs.length
      ? '<div class="compare-gucs"><span class="compare-gucs-label">Изменённые параметры</span> ' +
        shownGucs
          .map(function (name) {
            return "<code>" + escapeHtml(name) + "</code>";
          })
          .join(" ") +
        (extraGucs > 0 ? " <span>и ещё " + extraGucs + "</span>" : "") +
        "</div>"
      : "";
    const reasonHints =
      Array.isArray(compare.confidence_hints) && compare.confidence_hints.length
        ? compare.confidence_hints
        : collectConfidenceHints(summary, rows, confidence);
    const reasonsHtml = reasonHints.length
      ? '<div class="compare-confidence-reasons">' +
        reasonHints
          .map(function (text) {
            return "<div>" + escapeHtml(text) + "</div>";
          })
          .join("") +
        "</div>"
      : "";
    const workloadWarning = (
      typeof compare.workload_weak === "boolean" ? compare.workload_weak : workloadIsWeak(workload)
    )
      ? '<div class="compare-warning">Сопоставимость нагрузки низкая — сравнивать осторожно, не трактовать Δ как эффект настройки.</div>'
      : "";

    root.innerHTML =
      '<div class="compare-head">' +
      '<span class="status-pill">mode <strong>' +
      escapeHtml(modeLabel) +
      "</strong></span>" +
      '<span class="status-pill">influence rows <strong>' +
      escapeHtml(String(influence.row_count || rows.length)) +
      "</strong></span>" +
      '<span class="status-pill">workload match <strong>' +
      escapeHtml(
        workload.workload_match_score == null
          ? "—"
          : Number(workload.workload_match_score).toFixed(3)
      ) +
      " · " +
      escapeHtml(String(workload.level || "unknown")) +
      "</strong></span>" +
      '<span class="status-pill">confidence <strong>' +
      escapeHtml(String(confidence.confidence || "—")) +
      " · " +
      escapeHtml(String(confidence.evidence_type || "—")) +
      "</strong></span>" +
      '<span class="status-pill">improved <strong>' +
      escapeHtml(String(improvedCount)) +
      "</strong> · degraded <strong>" +
      escapeHtml(String(degradedCount)) +
      "</strong></span>" +
      "</div>" +
      gucHtml +
      reasonsHtml +
      extraSeriesHtml +
      oracleHtml +
      workloadWarning +
      (showManyChangesWarning
        ? '<div class="compare-warning">Одновременно изменено слишком много параметров (' +
          escapeHtml(String(changedCount)) +
          "), причинная достоверность снижена.</div>"
        : "") +
      genericInfluenceTableHtml;
    root.hidden = false;
    root.querySelectorAll("[data-compare-filter]").forEach(function (el) {
      el.addEventListener("change", function () {
        const key = el.getAttribute("data-compare-filter");
        if (key) compareFilterState[key] = el.value;
        if (lastCompareSummary) renderCompareInsights(lastCompareSummary);
      });
    });
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

  function stopLlmPoll() {
    if (llmPollTimer) {
      clearTimeout(llmPollTimer);
      llmPollTimer = null;
    }
  }

  function setLlmStatus(status) {
    const key = LLM_STATUS_RU[status] ? status : "idle";
    if (!els.llmStatus) return;
    els.llmStatus.className = "status-pill llm-status-" + key;
    els.llmStatus.innerHTML = "статус <strong>" + escapeHtml(LLM_STATUS_RU[key]) + "</strong>";
    if (els.llmSpinner) {
      if (key === "queued" || key === "running") {
        els.llmSpinner.classList.add("visible");
      } else {
        els.llmSpinner.classList.remove("visible");
      }
    }
    if (els.llmRunBtn) {
      els.llmRunBtn.disabled = key === "queued" || key === "running";
    }
  }

  function setLlmPolicyBadge(policy) {
    if (!els.llmPolicy) return;
    const info = policy && typeof policy === "object" ? policy : { name: String(policy || "none") };
    const name = info.name || "none";
    const sanitization = !!info.sanitization || !!info.enabled;
    els.llmPolicy.innerHTML =
      "policy <strong>" +
      escapeHtml(name) +
      "</strong>" +
      (sanitization ? " · sanitization" : "");
    const details = [];
    if ((info.deny_sections || []).length) {
      details.push("deny " + info.deny_sections.join(", "));
    }
    if ((info.dropped_sections || []).length) {
      details.push("убрано " + info.dropped_sections.join(", "));
    }
    const hits = info.mask_hits || {};
    const hitNames = Object.keys(hits).filter((key) => hits[key] > 0);
    if (hitNames.length) {
      details.push("mask " + hitNames.map((key) => key + "×" + hits[key]).join(", "));
    }
    els.llmPolicy.title = details.join(" · ") || "payload без фильтрации";
  }

  function setLlmQuality(job) {
    if (!els.llmQuality) return;
    const score = job && job.quality_score;
    const verdict = job && job.quality_verdict;
    if (score == null && !verdict) {
      els.llmQuality.hidden = true;
      return;
    }
    els.llmQuality.hidden = false;
    const klass = verdict === "fail" ? "fail" : verdict === "warning" ? "warning" : "success";
    els.llmQuality.className = "status-pill llm-quality-" + klass;
    els.llmQuality.innerHTML =
      "quality <strong>" +
      escapeHtml(String(score != null ? score : "—")) +
      "/100 · " +
      escapeHtml(String(verdict || "—")) +
      "</strong>";
    els.llmQuality.title = (job.quality_reasons || []).join(" | ");
    if (els.llmPublishNote) {
      els.llmPublishNote.hidden = job.publishable !== false;
    }
  }

  function resetLlmPanel() {
    stopLlmPoll();
    setLlmStatus("idle");
    if (els.llmMeta) els.llmMeta.textContent = "";
    if (els.llmAnswer) {
      els.llmAnswer.value = "";
      els.llmAnswer.hidden = true;
    }
    if (els.llmAnswerActions) els.llmAnswerActions.hidden = true;
    if (els.llmExtra) els.llmExtra.value = "";
    if (els.llmQuality) els.llmQuality.hidden = true;
    if (els.llmPublishNote) els.llmPublishNote.hidden = true;
  }

  function updateLlmProviderHint() {
    if (!els.llmProvider || !els.llmProviderHint) return;
    const option = els.llmProvider.selectedOptions[0];
    if (!option) {
      els.llmProviderHint.textContent = "";
      return;
    }
    const type = option.getAttribute("data-type") || "";
    const url = option.getAttribute("data-url") || "";
    const tokenEnv = option.getAttribute("data-token-env") || "";
    const tokenPresent = option.getAttribute("data-token-present") === "1";
    const bits = [];
    if (type === "dry_run") {
      bits.push("без обращения к модели");
    } else if (url) {
      bits.push(url);
    }
    if (tokenEnv && !tokenPresent) {
      bits.push("токен " + tokenEnv + " не задан — запрос упадёт");
    }
    els.llmProviderHint.textContent = bits.join(" · ");
  }

  async function loadLlmCatalog() {
    if (!els.llmTask || !els.llmProvider) return;
    try {
      const fetches = [fetch(apiBase + "/api/llm/policy")];
      if (!llmCatalogLoaded) {
        fetches.unshift(
          fetch(apiBase + "/api/llm/tasks"),
          fetch(apiBase + "/api/llm/providers")
        );
      }
      const responses = await Promise.all(fetches);
      let policyRes;
      if (!llmCatalogLoaded) {
        const [tasksRes, providersRes, loadedPolicyRes] = responses;
        policyRes = loadedPolicyRes;
        const tasksData = tasksRes.ok ? await tasksRes.json() : { tasks: [] };
        const providersData = providersRes.ok ? await providersRes.json() : { providers: [] };
        els.llmTask.innerHTML = (tasksData.tasks || [])
          .map((task) => {
            const selected = task.task === "summary" ? " selected" : "";
            return (
              "<option value=\"" +
              escapeHtml(task.task) +
              "\"" +
              selected +
              ">" +
              escapeHtml(task.title || task.task) +
              "</option>"
            );
          })
          .join("");
        els.llmProvider.innerHTML = (providersData.providers || [])
          .map((row) => {
            const selected = row.is_default ? " selected" : "";
            return (
              "<option value=\"" +
              escapeHtml(row.provider) +
              "\" data-type=\"" +
              escapeHtml(row.type || "") +
              "\" data-url=\"" +
              escapeHtml(row.base_url || "") +
              "\" data-token-env=\"" +
              escapeHtml(row.token_env || "") +
              "\" data-token-present=\"" +
              (row.token_present ? "1" : "0") +
              "\"" +
              selected +
              ">" +
              escapeHtml(row.provider) +
              (row.is_default ? " (по умолчанию)" : "") +
              "</option>"
            );
          })
          .join("");
        llmCatalogLoaded = true;
        updateLlmProviderHint();
      } else {
        policyRes = responses[0];
      }
      const policyData = policyRes.ok ? await policyRes.json() : { policy: { name: "none" } };
      setLlmPolicyBadge(policyData.policy || { name: "none" });
    } catch (_) {
      if (els.llmProviderHint) {
        els.llmProviderHint.textContent = "не удалось загрузить список провайдеров";
      }
      setLlmPolicyBadge({ name: "none" });
    }
  }

  function renderLlmJob(job) {
    const status = job.status || "idle";
    const visual =
      status === "success" && job.publishable === false ? "blocked" : status;
    setLlmStatus(visual);
    setLlmQuality(job);
    const bits = [];
    if (job.provider) bits.push("провайдер " + job.provider);
    if (job.model) bits.push("модель " + job.model);
    if (job.trace_id) bits.push("trace " + job.trace_id);
    if (job.latency_ms != null) bits.push(job.latency_ms + " мс");
    if (job.char_count) bits.push(job.char_count + " символов промпта");
    if (job.policy) {
      setLlmPolicyBadge(job.policy);
      const policyName = job.policy.name || "none";
      bits.push("policy " + policyName);
    }
    if (job.quality_score != null) {
      bits.push("quality " + job.quality_score + "/100");
    }
    if (job.publishable === false) bits.push("публикация закрыта");
    if (job.error && job.error.message) bits.push(job.error.error + ": " + job.error.message);
    if (els.llmMeta) els.llmMeta.textContent = bits.join(" · ");
  }

  async function loadQualityReport() {
    if (!sessionId || !els.qualityText) return;
    try {
      const res = await fetch(apiBase + "/api/sessions/" + sessionId + "/quality");
      const data = await res.json();
      if (!res.ok) return;
      els.qualityText.value = data.quality_text || "";
    } catch (_) {
      /* quality tab stays at the last analysis snapshot */
    }
  }

  async function loadLlmAnswer() {
    if (!sessionId || !els.llmAnswer) return;
    try {
      const res = await fetch(apiBase + "/api/sessions/" + sessionId + "/llm/answer");
      const data = await res.json();
      if (!res.ok) return;
      els.llmAnswer.value = data.text || "";
      els.llmAnswer.hidden = false;
      if (els.llmAnswerActions) els.llmAnswerActions.hidden = false;
    } catch (_) {
      /* status already shows the failure */
    }
  }

  async function pollLlmJob() {
    if (!sessionId) return;
    try {
      const res = await fetch(apiBase + "/api/sessions/" + sessionId + "/llm");
      const job = await res.json();
      if (!res.ok) {
        setLlmStatus("fail");
        if (els.llmMeta) els.llmMeta.textContent = job.error || "не удалось получить статус";
        return;
      }
      renderLlmJob(job);
      if (job.status === "success") {
        await loadLlmAnswer();
        await loadQualityReport();
        showToast(
          job.publishable === false
            ? "ответ готов, публикация заблокирована"
            : "ответ Qwen готов"
        );
        return;
      }
      if (job.status === "fail") {
        showError((job.error && job.error.message) || "ошибка LLM");
        return;
      }
      if (job.status === "queued" || job.status === "running") {
        llmPollTimer = setTimeout(pollLlmJob, 800);
      }
    } catch (err) {
      setLlmStatus("fail");
      if (els.llmMeta) els.llmMeta.textContent = String(err.message || err);
    }
  }

  async function startLlmJob() {
    showError("");
    if (!sessionId) {
      showError("сначала выполните анализ отчёта");
      return;
    }
    const task = (els.llmTask && els.llmTask.value) || "summary";
    const provider = (els.llmProvider && els.llmProvider.value) || "";
    const extra = (els.llmExtra && els.llmExtra.value.trim()) || "";
    setLlmStatus("queued");
    if (els.llmAnswer) {
      els.llmAnswer.value = "";
      els.llmAnswer.hidden = true;
    }
    if (els.llmAnswerActions) els.llmAnswerActions.hidden = true;
    try {
      const res = await fetch(apiBase + "/api/sessions/" + sessionId + "/llm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task: task,
          provider: provider,
          extra_instructions: extra,
        }),
      });
      const job = await res.json();
      if (!res.ok) {
        setLlmStatus("fail");
        showError(job.error || "не удалось запустить Qwen");
        renderLlmJob(job);
        return;
      }
      renderLlmJob(job);
      stopLlmPoll();
      llmPollTimer = setTimeout(pollLlmJob, 400);
    } catch (err) {
      setLlmStatus("fail");
      showError(String(err.message || err));
    }
  }

  function bindLlmPanel(data) {
    if (!els.llmPanel) return;
    const isJvm = (data.mode || data.scenario) === "jvm";
    if (isJvm || !llmUiAvailable) {
      resetLlmPanel();
      els.llmPanel.hidden = true;
      return;
    }
    els.llmPanel.hidden = false;
    resetLlmPanel();
    loadLlmCatalog();
  }

  function showResult(data) {
    sessionId = data.session_id;
    els.resultPanel.classList.add("visible");
    lastWikiText = data.wiki_text || "";
    els.wikiText.value = lastWikiText;
    els.promptText.value = data.prompt_text || "";
    els.briefText.value = data.brief_text || "";
    if (els.qualityText) els.qualityText.value = data.quality_text || "";
    setWikiMode("source");

    const summary = data.summary || {};
    const counts = summary.severity_counts || {};
    const pills = [];
    pills.push(
      '<span class="status-pill">сценарий <strong>' +
        escapeHtml(data.scenario || "") +
        "</strong></span>"
    );
    if (summary.oracle && summary.oracle.verdict) {
      const verdict = String(summary.oracle.verdict);
      const skipped = summary.oracle.skipped ? " · пропущен" : "";
      const layerTitle = (summary.oracle.layers || [])
        .map(function (layer) {
          if (!layer || !layer.name) return "";
          return (
            layer.name +
            "=" +
            (layer.verdict || "?") +
            (layer.skipped ? " skipped" : "")
          );
        })
        .filter(Boolean)
        .join("; ");
      const reasonTitle = (summary.oracle.reasons || []).join(" | ");
      if (llmUiAvailable) {
        pills.push(
          '<button type="button" class="status-pill pill-btn oracle-verdict oracle-' +
            escapeHtml(verdict) +
            '" data-tab="quality" title="' +
            escapeHtml([layerTitle, reasonTitle].filter(Boolean).join(" — ")) +
            '">oracle <strong>' +
            escapeHtml(verdict) +
            skipped +
            "</strong></button>"
        );
      } else {
        pills.push(
          '<span class="status-pill oracle-verdict oracle-' +
            escapeHtml(verdict) +
            '" title="' +
            escapeHtml([layerTitle, reasonTitle].filter(Boolean).join(" — ")) +
            '">oracle <strong>' +
            escapeHtml(verdict) +
            skipped +
            "</strong></span>"
        );
      }
    }
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
        const tab = btn.getAttribute("data-tab");
        if (tab) {
          activateTab(tab);
          return;
        }
        const sev = btn.getAttribute("data-sev");
        severityFilter = severityFilter === sev ? null : sev;
        renderFindingsCards(data.findings_ui || [], severityFilter);
      });
    });

    renderCompareInsights(summary);
    renderFindingsCards(data.findings_ui || [], severityFilter);
    bindCheckFlow(sessionId);

    els.downloadWiki.href = apiBase + "/api/sessions/" + sessionId + "/wiki";
    els.downloadWiki.download = data.wiki || "confluence.wiki";
    els.downloadZip.href = apiBase + "/api/sessions/" + sessionId + "/zip";
    bindLlmPanel(data);
  }

  async function runAnalysis() {
    showError("");
    if (isJvmMode()) {
      if (!selectedJvmSystemName()) {
        showError(
          isJvmNewSystem()
            ? "сначала подгрузите файлы новой АС"
            : "выберите АС для check jvm"
        );
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
      const metricMeta = jvmMetricMeta();
      els.runBtn.disabled = true;
      els.runSpinner.classList.add("visible");
      els.runHint.textContent = "анализ…";
      const meta = {
        mode: "jvm",
        system_name: selectedJvmSystemName(),
        pod_name: (els.jvmPodName && els.jvmPodName.value) || null,
        container_name: els.jvmContainerName.value,
        tree: jvmTreeAnswers(),
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
    if (adv && scenario === "nt_runs" && symptoms.length === 0) {
      showError(
        "Для сценария «Несколько прогонов НТ» выберите хотя бы один симптом (например, high_cpu или high_wal)."
      );
      return;
    }

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

  function _intOrNull(value) {
    const n = _numberOrNull(value);
    if (n == null) return null;
    return Math.trunc(n);
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
  if (els.jvmNewDropzone && els.jvmNewFileInput) {
    els.jvmNewDropzone.addEventListener("click", () => els.jvmNewFileInput.click());
    els.jvmNewDropzone.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        els.jvmNewFileInput.click();
      }
    });
    els.jvmNewFileInput.addEventListener("change", () => {
      addJvmFiles(els.jvmNewFileInput.files);
      els.jvmNewFileInput.value = "";
    });
    ["dragenter", "dragover"].forEach((ev) => {
      els.jvmNewDropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        els.jvmNewDropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((ev) => {
      els.jvmNewDropzone.addEventListener(ev, (e) => {
        e.preventDefault();
        els.jvmNewDropzone.classList.remove("dragover");
      });
    });
    els.jvmNewDropzone.addEventListener("drop", (e) => {
      addJvmFiles(e.dataTransfer.files);
    });
  }
  if (els.jvmCreateSystemBtn) {
    els.jvmCreateSystemBtn.addEventListener("click", createJvmSystemFromUi);
  }
  if (els.jvmNewSystemName) {
    els.jvmNewSystemName.addEventListener("input", updateModeUi);
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
  const copyQuality = document.getElementById("copy-quality");
  if (copyQuality) {
    copyQuality.addEventListener("click", () =>
      copyText(els.qualityText ? els.qualityText.value : "", "отчёт качества скопирован")
    );
  }
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
        if (els.jvmReviewConfirm) els.jvmReviewConfirm.checked = false;
        updateModeUi();
        updateScenarioHints();
      });
    });
  }
  if (els.jvmSystemName) {
    els.jvmSystemName.addEventListener("change", async () => {
      await loadJvmContainers();
      await loadJvmLastInput();
      if (els.jvmReviewConfirm) els.jvmReviewConfirm.checked = false;
      updateModeUi();
    });
  }
  if (els.jvmPodName) {
    els.jvmPodName.addEventListener("change", async () => {
      renderJvmPodAndContainerSelectors();
      await loadJvmLastInput();
      if (els.jvmReviewConfirm) els.jvmReviewConfirm.checked = false;
      updateModeUi();
    });
  }
  if (els.jvmContainerName) {
    els.jvmContainerName.addEventListener("change", async () => {
      await loadJvmLastInput();
      if (els.jvmReviewConfirm) els.jvmReviewConfirm.checked = false;
      updateModeUi();
    });
  }
  [
    els.jvmPodsPerShoulder,
    els.jvmRestartKind,
    els.jvmHeapGrowing,
    els.jvmMemoryCauseClosed,
    els.jvmOldgenMode,
    els.jvmOldgenUsedMib,
    els.jvmOldgenCapacity,
    els.jvmGrowthOf,
    els.jvmHeapGrowthPercent,
    els.jvmHeapGrowthHours,
    els.jvmGcRan,
    els.jvmHeapBeforeGc,
    els.jvmHeapAfterGc,
    els.jvmOldgenReturned,
    els.jvmCpuThrottled,
    els.jvmCpuPctShoulder1,
    els.jvmCpuPctShoulder2,
    els.jvmGcP95,
    els.jvmUserLatencyGrew,
    els.jvmUserLatencyP95,
    els.jvmPausesCoincide,
    els.jvmPostGcFloor,
    els.jvmGcCpuSpike,
    els.jvmHeapUsed,
    els.jvmOldgenUsedPercent,
    els.jvmMemoryUsagePercent,
  ].forEach((input) => {
    if (input) {
      input.addEventListener("input", () => {
        if (els.jvmReviewConfirm) els.jvmReviewConfirm.checked = false;
        updateModeUi();
      });
      input.addEventListener("change", () => {
        if (els.jvmReviewConfirm) els.jvmReviewConfirm.checked = false;
        updateModeUi();
      });
    }
  });
  if (els.jvmReviewConfirm) {
    els.jvmReviewConfirm.addEventListener("change", updateModeUi);
  }
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
  if (els.llmProvider) {
    els.llmProvider.addEventListener("change", updateLlmProviderHint);
  }
  if (els.llmRunBtn) {
    els.llmRunBtn.addEventListener("click", startLlmJob);
  }
  if (els.copyLlmAnswer) {
    els.copyLlmAnswer.addEventListener("click", () => {
      const blocked = Boolean(els.llmPublishNote && !els.llmPublishNote.hidden);
      copyText(
        els.llmAnswer && els.llmAnswer.value,
        blocked
          ? "скопировано для отладки — не для Confluence, quality gate закрыт"
          : "ответ скопирован"
      );
    });
  }

  setTabs();
  loadQwenStatus();
  loadSymptoms();
  loadJvmSystems();
  loadJvmPlaybook();
  loadJvmLastInput();
  updateScenarioHints();
})();
