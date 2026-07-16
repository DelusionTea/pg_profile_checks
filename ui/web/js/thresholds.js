/* global API_BASE */
(function () {
  "use strict";

  const apiBase = typeof API_BASE === "string" ? API_BASE : "";
  const root = document.getElementById("thresholds-root");
  const toc = document.getElementById("thresholds-toc");
  const sourceEl = document.getElementById("thresholds-source");
  const errorBanner = document.getElementById("error-banner");

  function showError(msg) {
    errorBanner.textContent = msg || "";
    errorBanner.classList.toggle("visible", !!msg);
  }

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function hintCell(row) {
    if (!row.has_hint) {
      return '<td class="hint-cell hint-empty">—</td>';
    }
    const parts = [];
    if (row.hint_when) {
      parts.push(
        "<div class=\"hint-block\"><span class=\"hint-label\">Когда менять</span>" +
          escapeHtml(row.hint_when) +
          "</div>"
      );
    }
    if (row.hint_databases) {
      parts.push(
        "<div class=\"hint-block\"><span class=\"hint-label\">Для каких БД</span>" +
          escapeHtml(row.hint_databases) +
          "</div>"
      );
    }
    if (row.hint_ref) {
      parts.push(
        "<div class=\"hint-ref\">" + escapeHtml(row.hint_ref) + "</div>"
      );
    }
    return '<td class="hint-cell">' + parts.join("") + "</td>";
  }

  function renderSection(section) {
    const wrap = document.createElement("section");
    wrap.className = "threshold-section";
    wrap.id = "sec-" + section.id;

    const h = document.createElement("h2");
    h.className = "section-title";
    h.textContent = section.title;
    wrap.appendChild(h);

    const table = document.createElement("table");
    table.className = "thresholds-table";
    table.innerHTML =
      "<thead><tr>" +
      "<th>Параметр</th><th>Значение</th><th>Тип</th>" +
      "<th>Справка (когда менять)</th>" +
      "</tr></thead>";
    const tbody = document.createElement("tbody");
    (section.rows || []).forEach(function (row) {
      const tr = document.createElement("tr");
      if (row.has_hint) tr.classList.add("has-hint");
      tr.innerHTML =
        '<td class="filename-cell">' +
        escapeHtml(row.key) +
        "</td><td><code>" +
        escapeHtml(row.value) +
        '</code></td><td class="type-cell">' +
        escapeHtml(row.type) +
        "</td>" +
        hintCell(row);
      tbody.appendChild(tr);
    });
    if (!(section.rows || []).length) {
      tbody.innerHTML =
        '<tr><td colspan="4" class="empty-row">нет параметров</td></tr>';
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  async function load() {
    showError("");
    try {
      const res = await fetch(apiBase + "/api/thresholds");
      const data = await res.json();
      if (!res.ok) {
        showError(data.error || "не удалось загрузить thresholds");
        root.innerHTML = "";
        return;
      }
      sourceEl.textContent = data.filename || "thresholds.yaml";
      sourceEl.title = data.source || "";
      toc.innerHTML = "";
      root.innerHTML = "";

      (data.sections || []).forEach(function (section) {
        const a = document.createElement("a");
        a.className = "toc-link";
        a.href = "#sec-" + encodeURIComponent(section.id);
        a.textContent = section.title;
        toc.appendChild(a);
        root.appendChild(renderSection(section));
      });

      if (!(data.sections || []).length) {
        root.innerHTML = '<p class="empty-row">файл порогов пуст</p>';
      }
    } catch (err) {
      showError(String(err.message || err));
      root.innerHTML = "";
    }
  }

  load();
})();
