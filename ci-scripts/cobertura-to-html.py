#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from source_index import build_source_index, load_text, resolve_source_file


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert a Cobertura XML report to a standalone HTML coverage report.",
    )
    parser.add_argument("input", help="Path to the Cobertura XML report")
    parser.add_argument(
        "output",
        help="Path to write the HTML report, or '-' for stdout",
    )
    parser.add_argument(
        "--source-root",
        action="append",
        default=[],
        dest="source_roots",
        metavar="DIR",
        help="Source directory to search (repeatable)",
    )
    parser.add_argument(
        "--sources-json",
        help="JSON map of filename to source text",
    )
    parser.add_argument(
        "--github-summary",
        help="Write a Markdown table to this file",
    )
    return parser.parse_args(argv)


def fnum(value: Optional[str], default: float = 0.0) -> float:
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def inum(value: Optional[str], default: int = 0) -> int:
    try:
        return int(float(value)) if value is not None else default
    except ValueError:
        return default


def line_stats(lines: List[Dict[str, Any]]) -> Dict[str, int]:
    covered = sum(1 for line in lines if line["hits"] > 0)
    branches = [line for line in lines if line["branch"]]
    branch_hit = sum(1 for line in branches if line["hits"] > 0)
    return {
        "lines_valid": len(lines),
        "lines_covered": covered,
        "branches_valid": len(branches),
        "branches_covered": branch_hit,
    }


def xml_source_roots(root: ET.Element) -> List[str]:
    roots = []
    for source in root.findall("sources/source"):
        text = (source.text or "").strip()
        if text:
            roots.append(text)
    return roots


def load_sources_json(path: Optional[Path]) -> Dict[str, str]:
    if path is None or not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items()}


def default_sidecar(xml_path: Path) -> Path:
    return xml_path.with_name(xml_path.stem + "-sources.json")


def parse_cobertura(
    path: Path,
    extra_roots: Iterable[str] = (),
    sources_json: Optional[Path] = None,
) -> Dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"Cobertura report not found: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError(f"Invalid Cobertura XML in {path}: {exc}") from exc
    if root.tag != "coverage":
        raise ValueError(f"Expected a Cobertura <coverage> root in {path}, found <{root.tag}>")

    xml_dir = path.parent.resolve()
    unique_roots: List[str] = []
    for item in list(extra_roots) + xml_source_roots(root) + [str(xml_dir), str(Path.cwd())]:
        if item and item not in unique_roots:
            unique_roots.append(item)

    embedded = load_sources_json(sources_json)
    if not embedded:
        embedded = load_sources_json(default_sidecar(path))
    indexed_files = build_source_index(unique_roots)

    files_with_source = 0
    fallback_classes_covered = 0
    fallback_classes_valid = 0
    fallback_methods_covered = 0
    fallback_methods_valid = 0
    packages: List[Dict[str, Any]] = []
    for pkg in root.findall("packages/package"):
        classes: List[Dict[str, Any]] = []
        for cls in pkg.findall("classes/class"):
            lines = []
            for line in cls.findall("lines/line"):
                lines.append(
                    {
                        "n": inum(line.get("number")),
                        "hits": inum(line.get("hits")),
                        "branch": line.get("branch") == "true",
                        "cond": line.get("condition-coverage") or "",
                    }
                )
            stats = line_stats(lines)
            filename = cls.get("filename") or ""
            source_text = embedded.get(filename)
            if source_text is None:
                source_path = resolve_source_file(filename, indexed_files)
                source_text = load_text(source_path) if source_path else None
            if source_text is not None:
                files_with_source += 1
            for method in cls.findall("methods/method"):
                fallback_methods_valid += 1
                if fnum(method.get("line-rate")) > 0:
                    fallback_methods_covered += 1
            fallback_classes_valid += 1
            if fnum(cls.get("line-rate")) > 0:
                fallback_classes_covered += 1
            classes.append(
                {
                    "name": cls.get("name") or "",
                    "filename": filename,
                    "line_rate": fnum(cls.get("line-rate")),
                    "branch_rate": fnum(cls.get("branch-rate")),
                    "lines": lines if source_text else [],
                    "source": source_text,
                    **stats,
                }
            )
        pkg_lines = [line for cls in classes for line in cls["lines"]]
        pkg_stats = line_stats(pkg_lines)
        packages.append(
            {
                "name": pkg.get("name") or "",
                "line_rate": fnum(pkg.get("line-rate")),
                "branch_rate": fnum(pkg.get("branch-rate")),
                "classes": sorted(classes, key=lambda item: item["filename"] or item["name"]),
                **pkg_stats,
            }
        )

    packages.sort(key=lambda item: item["name"])
    timestamp = inum(root.get("timestamp"))
    generated = (
        datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        if timestamp
        else ""
    )
    if root.get("classes-covered") is None:
        classes_covered, classes_valid = fallback_classes_covered, fallback_classes_valid
    else:
        classes_covered = inum(root.get("classes-covered"))
        classes_valid = inum(root.get("classes-valid"))
    if root.get("methods-covered") is None:
        methods_covered, methods_valid = fallback_methods_covered, fallback_methods_valid
    else:
        methods_covered = inum(root.get("methods-covered"))
        methods_valid = inum(root.get("methods-valid"))
    if root.get("instructions-covered") is None:
        instructions_covered, instructions_valid = None, None
    else:
        instructions_covered = inum(root.get("instructions-covered"))
        instructions_valid = inum(root.get("instructions-valid"))
    return {
        "source": str(path),
        "generated": generated,
        "line_rate": fnum(root.get("line-rate")),
        "branch_rate": fnum(root.get("branch-rate")),
        "lines_covered": inum(root.get("lines-covered")),
        "lines_valid": inum(root.get("lines-valid")),
        "branches_covered": inum(root.get("branches-covered")),
        "branches_valid": inum(root.get("branches-valid")),
        "classes_covered": classes_covered,
        "classes_valid": classes_valid,
        "methods_covered": methods_covered,
        "methods_valid": methods_valid,
        "instructions_covered": instructions_covered,
        "instructions_valid": instructions_valid,
        "files_with_source": files_with_source,
        "packages": packages,
    }


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Code coverage report</title>
  <style>
    :root {
      --bg: #f4f6f8;
      --card: #ffffff;
      --ink: #1c2430;
      --muted: #5c6b7a;
      --line: #d8dee6;
      --good: #1b7f4e;
      --good-bg: #e5f6ec;
      --ok: #9a6700;
      --ok-bg: #fff4d6;
      --bad: #b42318;
      --bad-bg: #fde8e6;
      --neutral: #f3f5f7;
      --accent: #1250c4;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 "Segoe UI", system-ui, sans-serif;
      color: var(--ink);
      background: var(--bg);
    }
    header {
      background: var(--card);
      border-bottom: 1px solid var(--line);
      padding: 24px 28px 18px;
    }
    h1 { margin: 0 0 4px; font-size: 22px; font-weight: 650; }
    .meta { color: var(--muted); font-size: 13px; }
    .kpis {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-top: 18px;
    }
    .kpi {
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 14px 16px;
    }
    .kpi .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    .kpi .value { font-size: 28px; font-weight: 700; margin: 4px 0 2px; }
    .kpi .sub { color: var(--muted); font-size: 12px; }
    .bar { height: 8px; background: #e8edf2; border-radius: 99px; overflow: hidden; margin-top: 10px; }
    .bar > span { display: block; height: 100%; }
    .good .value, .good .fill { color: var(--good); background: var(--good); }
    .ok .value, .ok .fill { color: var(--ok); background: var(--ok); }
    .bad .value, .bad .fill { color: var(--bad); background: var(--bad); }
    .good .value, .ok .value, .bad .value { background: none; }
    main { padding: 18px 28px 40px; }
    .toolbar {
      display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-bottom: 12px;
    }
    input[type="search"], select {
      border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; font: inherit; background: var(--card);
    }
    input[type="search"] { min-width: 280px; flex: 1; }
    .seg {
      display: flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--card);
    }
    .seg label {
      padding: 8px 12px; cursor: pointer; font-size: 13px; border-right: 1px solid var(--line);
    }
    .seg label:last-child { border-right: 0; }
    .seg input { display: none; }
    .seg input:checked + span { color: var(--accent); font-weight: 650; }
    table { width: 100%; border-collapse: collapse; background: var(--card); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
    th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
    th { font-size: 12px; color: var(--muted); font-weight: 600; background: #fafbfc; }
    th.sortable { cursor: pointer; user-select: none; white-space: nowrap; }
    th.sortable:hover { color: var(--ink); }
    th .arrow { color: #b0bac4; font-size: 11px; margin-left: 4px; }
    th.active .arrow { color: var(--accent); }
    tr.pkg { cursor: pointer; }
    tr.pkg:hover { background: #f7f9fb; }
    .name { font-family: ui-monospace, Consolas, monospace; font-size: 12.5px; }
    .pct { font-variant-numeric: tabular-nums; font-weight: 650; white-space: nowrap; }
    .chip { display: inline-block; min-width: 58px; text-align: center; border-radius: 999px; padding: 2px 8px; font-size: 12px; font-weight: 650; }
    .chip.good { background: var(--good-bg); color: var(--good); }
    .chip.ok { background: var(--ok-bg); color: var(--ok); }
    .chip.bad { background: var(--bad-bg); color: var(--bad); }
    .mini { width: 110px; height: 7px; background: #e8edf2; border-radius: 99px; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 8px; }
    .mini > span { display: block; height: 100%; }
    .mini.good > span { background: var(--good); }
    .mini.ok > span { background: var(--ok); }
    .mini.bad > span { background: var(--bad); }
    .files { background: #f8fafc; }
    .files td { padding: 0; }
    .file-row { display: grid; grid-template-columns: 1fr 140px 140px 110px; gap: 8px; padding: 8px 12px 8px 28px; border-top: 1px solid var(--line); }
    .file-row.has-source { cursor: pointer; }
    .file-row.has-source:hover { background: #eef3f8; }
    .src {
      font-family: ui-monospace, Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
      padding: 8px 0 12px;
      overflow: auto;
      max-height: 560px;
      border-top: 1px solid var(--line);
      background: #fff;
    }
    .src-line { display: grid; grid-template-columns: 56px 64px minmax(0, 1fr); }
    .src-line .gutter, .src-line .hits {
      text-align: right; padding: 0 10px; color: var(--muted); user-select: none; border-right: 1px solid var(--line);
    }
    .src-line .code { padding: 0 12px; white-space: pre; overflow: hidden; }
    .src-line .kw { color: #0033b3; }
    .src-line .str { color: #067d17; }
    .src-line .com { color: #8c8c8c; font-style: italic; }
    .src-line .typ { color: #871094; }
    .src-line.hit { background: var(--good-bg); }
    .src-line.miss { background: var(--bad-bg); }
    .src-line.partial { background: var(--ok-bg); }
    .src-line.hit .hits { color: var(--good); }
    .src-line.miss .hits { color: var(--bad); }
    .empty { color: var(--muted); padding: 16px; }
  </style>
</head>
<body>
  <header>
    <h1>Code coverage report</h1>
    <div class="meta" id="meta"></div>
    <div class="kpis" id="kpis"></div>
  </header>
  <main>
    <div class="toolbar">
      <input id="q" type="search" placeholder="Filter packages or files…">
      <select id="sort">
        <option value="name">Sort by package name</option>
        <option value="line">Sort by line coverage</option>
        <option value="branch">Sort by branch coverage</option>
        <option value="files">Sort by files</option>
      </select>
      <select id="dir">
        <option value="asc">Ascending</option>
        <option value="desc">Descending</option>
      </select>
      <div class="seg">
        <label><input type="radio" name="cov" value="all" checked><span>All</span></label>
        <label><input type="radio" name="cov" value="uncovered"><span>Uncovered only</span></label>
        <label><input type="radio" name="cov" value="covered"><span>Covered only</span></label>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th class="sortable" data-sort="name">Package <span class="arrow"></span></th>
          <th class="sortable" data-sort="line">Line <span class="arrow"></span></th>
          <th class="sortable" data-sort="branch">Branch <span class="arrow"></span></th>
          <th class="sortable" data-sort="files">Files <span class="arrow"></span></th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </main>
  <script id="coverage-data" type="application/json">__DATA__</script>
  <script>
    const data = JSON.parse(document.getElementById("coverage-data").textContent);
    const pct = (n) => (n * 100).toFixed(1) + "%";
    const bucket = (n) => n >= 0.8 ? "good" : n >= 0.5 ? "ok" : "bad";
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
    const state = { sort: "name", dir: "asc", openPkg: null, openFile: null };

    document.getElementById("meta").textContent =
      data.source + (data.generated ? " · generated " + data.generated : "");

    function rate(covered, valid) {
      if (covered == null || valid == null || Number(valid) <= 0) return null;
      return Number(covered) / Number(valid);
    }
    const kpis = [
      ["Class", data.classes_covered, data.classes_valid],
      ["Method", data.methods_covered, data.methods_valid],
      ["Branch", data.branches_covered, data.branches_valid],
      ["Line", data.lines_covered, data.lines_valid],
      ["Instruction", data.instructions_covered, data.instructions_valid],
    ].filter(([, covered, valid]) => rate(covered, valid) != null);
    document.getElementById("kpis").innerHTML = kpis.map(([label, covered, valid]) => {
      const value = rate(covered, valid);
      return `
      <div class="kpi ${bucket(value)}">
        <div class="label">${label}</div>
        <div class="value">${pct(value)}</div>
        <div class="sub">${covered} / ${valid}</div>
        <div class="bar"><span class="fill" style="width:${(value * 100).toFixed(1)}%;background:currentColor"></span></div>
      </div>`;
    }).join("");

    function covFilter() {
      const selected = document.querySelector('input[name="cov"]:checked');
      return selected ? selected.value : "all";
    }

    function cmp(a, b) {
      let result = 0;
      if (state.sort === "line") result = a.line_rate - b.line_rate;
      else if (state.sort === "branch") result = a.branch_rate - b.branch_rate;
      else if (state.sort === "files") result = a.classes.length - b.classes.length;
      else result = a.name.localeCompare(b.name);
      return state.dir === "desc" ? -result : result;
    }

    function cmpFile(a, b) {
      let result = 0;
      if (state.sort === "line") result = a.line_rate - b.line_rate;
      else if (state.sort === "branch") result = a.branch_rate - b.branch_rate;
      else if (state.sort === "files") result = a.lines_valid - b.lines_valid;
      else result = (a.filename || a.name).localeCompare(b.filename || b.name);
      return state.dir === "desc" ? -result : result;
    }

    function coverageClass(line) {
      if (!line) return "";
      if (line.branch && line.cond && line.cond.indexOf("100%") !== 0 && line.hits > 0) return "partial";
      return line.hits > 0 ? "hit" : "miss";
    }

    function highlight(text, filename) {
      const ext = (filename.split(".").pop() || "").toLowerCase();
      const keywords = {
        kt: "package import class data object interface fun val var if else when for while return private public internal protected override abstract sealed const null true false this super in is as",
        java: "package import class interface enum public private protected static final void return if else for while new null true false this super extends implements",
        swift: "import class struct enum protocol func var let if else switch for while return public private internal fileprivate open override nil true false self",
        cpp: "include class struct enum namespace template typename void int return if else for while public private protected const nullptr true false this",
        c: "include void int return if else for while const struct enum typedef",
        h: "include void int return if else for while const struct enum typedef class namespace",
        cs: "using namespace class struct interface public private protected static void return if else for while new null true false this override",
        go: "package import func var const type struct interface return if else for range nil true false",
        py: "import from class def return if elif else for while in not and or None True False self yield async await",
        ts: "import export class interface type function const let var return if else for while new null true false this async await",
        js: "import export class function const let var return if else for while new null true false this async await",
      }[ext] || "";
      const kw = new Set(keywords.split(" ").filter(Boolean));
      const out = [];
      let i = 0;
      const push = (cls, value) => out.push(cls ? `<span class="${cls}">${esc(value)}</span>` : esc(value));
      while (i < text.length) {
        if (text.startsWith("//", i) || (text.startsWith("#", i) && ext === "py")) {
          const end = text.indexOf("\n", i);
          const take = end === -1 ? text.length : end;
          push("com", text.slice(i, take));
          i = take;
          continue;
        }
        if (text.startsWith("/*", i)) {
          const end = text.indexOf("*/", i + 2);
          const take = end === -1 ? text.length : end + 2;
          push("com", text.slice(i, take));
          i = take;
          continue;
        }
        const quote = text[i];
        if (quote === '"' || quote === "'" || quote === "`") {
          let j = i + 1;
          while (j < text.length && text[j] !== quote) {
            if (text[j] === "\\") j += 2;
            else j += 1;
          }
          push("str", text.slice(i, Math.min(text.length, j + 1)));
          i = Math.min(text.length, j + 1);
          continue;
        }
        const ident = text.slice(i).match(/^[A-Za-z_][A-Za-z0-9_]*/);
        if (ident) {
          const word = ident[0];
          const cls = kw.has(word) ? "kw" : (/^[A-Z]/.test(word) ? "typ" : "");
          push(cls, word);
          i += word.length;
          continue;
        }
        push("", text[i]);
        i += 1;
      }
      return out.join("");
    }

    function renderSource(cls) {
      if (!cls.source) return "";
      const byLine = {};
      (cls.lines || []).forEach((line) => { byLine[line.n] = line; });
      const rows = cls.source.split("\n");
      if (rows.length && rows[rows.length - 1] === "") rows.pop();
      return `<div class="src">${rows.map((text, i) => {
        const n = i + 1;
        const line = byLine[n];
        const kind = coverageClass(line);
        const hits = line ? String(line.hits) : "";
        return `<div class="src-line ${kind}"><span class="gutter">${n}</span><span class="hits">${hits}</span><span class="code">${highlight(text, cls.filename || cls.name)}</span></div>`;
      }).join("")}</div>`;
    }

    function updateHeaderArrows() {
      document.querySelectorAll("th.sortable").forEach((th) => {
        const key = th.getAttribute("data-sort");
        const arrow = th.querySelector(".arrow");
        const active = key === state.sort;
        th.classList.toggle("active", active);
        arrow.textContent = active ? (state.dir === "asc" ? "▲" : "▼") : "▲▼";
      });
      document.getElementById("sort").value = state.sort;
      document.getElementById("dir").value = state.dir;
    }

    function render() {
      updateHeaderArrows();
      const q = document.getElementById("q").value.toLowerCase().trim();
      const filter = covFilter();
      let pkgs = data.packages.slice();
      if (filter === "uncovered") pkgs = pkgs.filter(p => p.line_rate === 0);
      if (filter === "covered") pkgs = pkgs.filter(p => p.line_rate > 0);
      if (q) {
        pkgs = pkgs.filter(p => p.name.toLowerCase().includes(q) || p.classes.some(c => (c.filename || c.name).toLowerCase().includes(q)));
      }
      pkgs.sort(cmp);
      const tbody = document.getElementById("rows");
      if (!pkgs.length) {
        tbody.innerHTML = '<tr><td colspan="4" class="empty">No packages match this filter.</td></tr>';
        return;
      }
      tbody.innerHTML = pkgs.map((pkg) => {
        const shown = state.openPkg === pkg.name;
        const files = shown ? `<tr class="files"><td colspan="4">${pkg.classes.slice().sort(cmpFile).map(cls => {
          const key = pkg.name + "::" + cls.filename;
          const open = state.openFile === key && cls.source;
          return `<div class="file-row${cls.source ? " has-source" : ""}" data-file="${cls.source ? esc(key) : ""}">
            <div class="name">${esc(cls.filename || cls.name)}</div>
            <div><span class="mini ${bucket(cls.line_rate)}"><span style="width:${(cls.line_rate*100).toFixed(1)}%"></span></span><span class="pct">${pct(cls.line_rate)}</span></div>
            <div><span class="mini ${bucket(cls.branch_rate)}"><span style="width:${(cls.branch_rate*100).toFixed(1)}%"></span></span><span class="pct">${pct(cls.branch_rate)}</span></div>
            <div>${cls.lines_covered}/${cls.lines_valid} lines</div>
          </div>${open ? renderSource(cls) : ""}`;
        }).join("")}</td></tr>` : "";
        return `<tr class="pkg" data-pkg="${esc(pkg.name)}">
          <td class="name">${shown ? "▾" : "▸"} ${esc(pkg.name)}</td>
          <td><span class="mini ${bucket(pkg.line_rate)}"><span style="width:${(pkg.line_rate*100).toFixed(1)}%"></span></span><span class="chip ${bucket(pkg.line_rate)}">${pct(pkg.line_rate)}</span></td>
          <td><span class="chip ${bucket(pkg.branch_rate)}">${pct(pkg.branch_rate)}</span></td>
          <td>${pkg.classes.length} · ${pkg.lines_covered}/${pkg.lines_valid} lines</td>
        </tr>${files}`;
      }).join("");
    }

    document.getElementById("rows").addEventListener("click", (e) => {
      const pkgRow = e.target.closest("tr.pkg");
      const fileRow = e.target.closest(".file-row");
      if (fileRow) {
        const key = fileRow.getAttribute("data-file");
        if (!key) return;
        state.openFile = state.openFile === key ? null : key;
        render();
        return;
      }
      if (pkgRow) {
        const name = pkgRow.getAttribute("data-pkg");
        state.openPkg = state.openPkg === name ? null : name;
        state.openFile = null;
        render();
      }
    });
    document.querySelectorAll("th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.getAttribute("data-sort");
        if (state.sort === key) state.dir = state.dir === "asc" ? "desc" : "asc";
        else { state.sort = key; state.dir = key === "name" ? "asc" : "desc"; }
        render();
      });
    });
    document.getElementById("q").addEventListener("input", render);
    document.getElementById("sort").addEventListener("change", (e) => { state.sort = e.target.value; render(); });
    document.getElementById("dir").addEventListener("change", (e) => { state.dir = e.target.value; render(); });
    document.querySelectorAll('input[name="cov"]').forEach((el) => el.addEventListener("change", render));
    render();
  </script>
</body>
</html>
"""


def format_metric(covered: Any, valid: Any) -> str:
    if covered is None or valid is None:
        return "—"
    try:
        covered_n = int(covered)
        valid_n = int(valid)
    except (TypeError, ValueError):
        return "—"
    if valid_n <= 0:
        return "—"
    return f"{(100.0 * covered_n / valid_n):.1f}% ({covered_n}/{valid_n})"


def github_summary_markdown(report: Dict[str, Any]) -> str:
    return (
        "## Coverage summary\n\n"
        "| Package | Class, % | Method, % | Branch, % | Line, % | Instruction, % |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        f"| all classes "
        f"| {format_metric(report.get('classes_covered'), report.get('classes_valid'))} "
        f"| {format_metric(report.get('methods_covered'), report.get('methods_valid'))} "
        f"| {format_metric(report.get('branches_covered'), report.get('branches_valid'))} "
        f"| {format_metric(report.get('lines_covered'), report.get('lines_valid'))} "
        f"| {format_metric(report.get('instructions_covered'), report.get('instructions_valid'))} |\n"
    )


def render_html(report: Dict[str, Any]) -> str:
    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c")
    return HTML_TEMPLATE.replace("__DATA__", payload)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    input_path = Path(args.input)
    report = parse_cobertura(
        input_path,
        args.source_roots,
        Path(args.sources_json) if args.sources_json else None,
    )
    html_doc = render_html(report)

    if args.github_summary:
        summary_path = Path(args.github_summary)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with summary_path.open("a", encoding="utf-8") as handle:
            handle.write(github_summary_markdown(report))

    if args.output == "-":
        sys.stdout.write(html_doc)
        return 0

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html_doc, encoding="utf-8")
    print(
        f"Wrote HTML coverage report to {output_path} "
        f"(line-rate={report['line_rate']:.2%}, "
        f"{report['lines_covered']}/{report['lines_valid']} lines, "
        f"{len(report['packages'])} packages, "
        f"{report['files_with_source']} files with source)"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (FileNotFoundError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
