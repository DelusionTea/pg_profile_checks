/*
 * Render a .wiki file through ui/web/js/wiki_preview.js and verify every table
 * row has the same cell count as its header, badges resolve, and pipe entities
 * are unescaped. Run with any JS shell that provides readFile/read, e.g.
 *   /System/Library/Frameworks/JavaScriptCore.framework/Versions/A/Helpers/jsc \
 *     scripts/check_wiki_preview.js -- <file.wiki> [more.wiki ...]
 */
(function () {
  "use strict";

  const readText =
    typeof readFile === "function"
      ? readFile
      : typeof read === "function"
        ? read
        : null;
  if (!readText) throw new Error("no file reading primitive in this JS shell");

  const argv = []
    .concat(globalThis.arguments ? Array.prototype.slice.call(globalThis.arguments) : [])
    .concat(globalThis.scriptArgs || [])
    .concat(globalThis.process ? globalThis.process.argv : []);
  const files = argv.filter((arg) => /\.wiki$/.test(String(arg)));
  if (!files.length) throw new Error("usage: check_wiki_preview.js -- <file.wiki>");

  const host = {};
  new Function("globalThis", readText("ui/web/js/wiki_preview.js"))(host);

  let failures = 0;
  files.forEach(function (file) {
    const html = host.WikiPreview.render(readText(file));
    const tables = html.match(/<table[\s\S]*?<\/table>/g) || [];
    let mismatched = 0;
    tables.forEach(function (table) {
      const headerCount = (table.match(/<th>/g) || []).length;
      (table.match(/<tr>[\s\S]*?<\/tr>/g) || []).forEach(function (row) {
        const cellCount = (row.match(/<td>/g) || []).length;
        if (cellCount && cellCount !== headerCount) {
          mismatched += 1;
          print("  MISMATCH " + cellCount + " cells vs " + headerCount + " headers: " + row.slice(0, 140));
        }
      });
    });
    const badges = (html.match(/class="wiki-status/g) || []).length;
    const rawMacros = (html.match(/\{status:/g) || []).length;
    const literalEntities = (html.match(/&amp;#124;/g) || []).length;
    const ok = mismatched === 0 && rawMacros === 0 && literalEntities === 0;
    if (!ok) failures += 1;
    print(
      "[" + (ok ? "OK" : "FAIL") + "] " + file +
        " tables=" + tables.length +
        " mismatched_rows=" + mismatched +
        " badges=" + badges +
        " unrendered_macros=" + rawMacros +
        " literal_pipe_entities=" + literalEntities
    );
  });

  if (failures) throw new Error(failures + " file(s) render with broken tables");
  print("Preview render OK for " + files.length + " file(s).");
})();
