/* Minimal Confluence Wiki Markup → HTML preview (subset). */
(function (global) {
  "use strict";

  function escapeHtml(text) {
    return String(text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function statusHtml(colour, title) {
    const c = (colour || "Grey").toLowerCase();
    return (
      '<span class="wiki-status wiki-status-' +
      escapeHtml(c) +
      '">' +
      escapeHtml(title || "") +
      "</span>"
    );
  }

  function inlineFormat(text) {
    let s = escapeHtml(text);
    s = s.replace(
      /\{status:colour=([^|]+)\|title=([^}|]+)[^}]*\}/g,
      function (_, colour, title) {
        return statusHtml(colour, title);
      }
    );
    s = s.replace(
      /\[([^\]|]+)\|(https?:\/\/[^\]\s]+|#[^\]\s]+)\]/g,
      function (_, label, href) {
        return (
          '<a href="' +
          escapeHtml(href) +
          '">' +
          label +
          "</a>"
        );
      }
    );
    s = s.replace(/\*([^*]+)\*/g, "<strong>$1</strong>");
    s = s.replace(/\{\{([^}]+)\}\}/g, "<code>$1</code>");
    return s;
  }

  function renderWiki(markup) {
    const lines = String(markup || "").split(/\r?\n/);
    const out = [];
    let i = 0;
    let inCode = false;
    let codeBuf = [];
    let inPanel = null;
    let panelBuf = [];
    let inExpand = null;
    let expandBuf = [];
    let inTable = false;

    function flushTable() {
      if (inTable) {
        out.push("</tbody></table>");
        inTable = false;
      }
    }

    function flushPanel() {
      if (!inPanel) return;
      out.push(
        '<div class="wiki-panel wiki-panel-' +
          escapeHtml(inPanel.macro) +
          '"><div class="wiki-panel-title">' +
          escapeHtml(inPanel.title) +
          '</div><div class="wiki-panel-body">' +
          panelBuf.join("") +
          "</div></div>"
      );
      inPanel = null;
      panelBuf = [];
    }

    function flushExpand() {
      if (!inExpand) return;
      out.push(
        "<details class=\"wiki-expand\"><summary>" +
          escapeHtml(inExpand.title) +
          "</summary><div class=\"wiki-expand-body\">" +
          expandBuf.join("") +
          "</div></details>"
      );
      inExpand = null;
      expandBuf = [];
    }

    function push(html) {
      if (inExpand) expandBuf.push(html);
      else if (inPanel) panelBuf.push(html);
      else out.push(html);
    }

    while (i < lines.length) {
      const raw = lines[i];
      const line = raw;

      if (inCode) {
        if (line.trim() === "{code}") {
          push(
            '<pre class="wiki-code"><code>' +
              escapeHtml(codeBuf.join("\n")) +
              "</code></pre>"
          );
          inCode = false;
          codeBuf = [];
        } else {
          codeBuf.push(line);
        }
        i += 1;
        continue;
      }

      const codeOpen = line.match(/^\{code(?::([^}]*))?\}/);
      if (codeOpen) {
        flushTable();
        inCode = true;
        codeBuf = [];
        i += 1;
        continue;
      }

      const panelOpen = line.match(/^\{(info|warning|note)(?::title=([^}]*))?\}/);
      if (panelOpen) {
        flushTable();
        flushPanel();
        inPanel = { macro: panelOpen[1], title: panelOpen[2] || panelOpen[1] };
        i += 1;
        continue;
      }
      if (inPanel && line.trim() === "{" + inPanel.macro + "}") {
        flushPanel();
        i += 1;
        continue;
      }

      if (inExpand && line.trim() === "{expand}") {
        flushExpand();
        i += 1;
        continue;
      }
      const expandTitleEq = line.match(/^\{expand:title=([^}]*)\}/);
      const expandShort = line.match(/^\{expand:([^}]+)\}/);
      const expandOpen = expandTitleEq || expandShort;
      if (expandOpen) {
        flushTable();
        flushExpand();
        inExpand = { title: expandOpen[1] || "Детали" };
        i += 1;
        continue;
      }

      if (/^\{toc/.test(line.trim())) {
        flushTable();
        push('<p class="wiki-toc-stub">[TOC]</p>');
        i += 1;
        continue;
      }

      if (line.trim() === "----") {
        flushTable();
        push("<hr>");
        i += 1;
        continue;
      }

      const h = line.match(/^(h([1-6]))\.\s+(.*)$/);
      if (h) {
        flushTable();
        push("<h" + h[2] + ">" + inlineFormat(h[3]) + "</h" + h[2] + ">");
        i += 1;
        continue;
      }

      if (line.indexOf("||") === 0) {
        flushTable();
        const cells = line.split("||").filter(Boolean);
        push(
          '<table class="wiki-table"><thead><tr>' +
            cells.map((c) => "<th>" + inlineFormat(c) + "</th>").join("") +
            "</tr></thead><tbody>"
        );
        inTable = true;
        i += 1;
        continue;
      }
      if (inTable && line.indexOf("|") === 0 && line.indexOf("||") !== 0) {
        const cells = line.split("|").filter((_, idx, arr) => idx > 0 && idx < arr.length);
        // split("|") on "|a|b|" → ["", "a", "b", ""] — take middle
        const parts = line.split("|");
        const mid = parts.slice(1, parts[parts.length - 1] === "" ? -1 : undefined);
        push(
          "<tr>" +
            mid.map((c) => "<td>" + inlineFormat(c) + "</td>").join("") +
            "</tr>"
        );
        i += 1;
        continue;
      }
      if (inTable) {
        flushTable();
      }

      if (/^\* /.test(line)) {
        push("<ul><li>" + inlineFormat(line.slice(2)) + "</li></ul>");
        i += 1;
        continue;
      }
      if (/^\*\* /.test(line)) {
        push(
          '<ul class="wiki-sub"><li>' + inlineFormat(line.slice(3)) + "</li></ul>"
        );
        i += 1;
        continue;
      }
      if (/^# /.test(line)) {
        push("<ol><li>" + inlineFormat(line.slice(2)) + "</li></ol>");
        i += 1;
        continue;
      }

      if (line.trim() === "") {
        push("");
        i += 1;
        continue;
      }

      push("<p>" + inlineFormat(line) + "</p>");
      i += 1;
    }

    flushTable();
    flushPanel();
    flushExpand();
    return '<div class="wiki-preview-inner">' + out.join("\n") + "</div>";
  }

  global.WikiPreview = { render: renderWiki };
})(typeof window !== "undefined" ? window : globalThis);
