"use strict";
/* JSON code editor for the robops dashboard (player data, DataStore values).

   A transparent <textarea> (native caret, selection, undo, IME, find) sits on top of a
   highlighted copy of the text. Around it: line numbers, markers for the lines changed
   since loading, the line of a JSON error, bracket matching and a status bar.
   Keys: Enter keeps/extends indentation, Tab / Shift+Tab indent, brackets and quotes
   close themselves, Ctrl+S saves, Shift+Alt+F formats, Ctrl+] / Ctrl+[ indent.
   Text is only ever inserted with textContent (the data comes from Roblox). */

const RobopsEditor = (() => {
  const INDENT = "  ";
  const PAIRS = { "{": "}", "[": "]", '"': '"' };
  const CLOSERS = new Set(["}", "]", '"']);
  const TOKEN = /("(?:[^"\\\n]|\\.)*"?)|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b|([{}[\],:])|(\s+)|([^\s"{}[\],:]+)/g;

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* Finds the first syntax error itself (browsers don't always give its position) and
     says what is wrong in plain words. Returns {at, message} or null if the JSON is valid. */
  function locateError(text) {
    let i = 0;
    const fail = (message, at = i) => { throw { at, message }; };
    const describe = (ch) => (ch === undefined ? "the end of the text" : ch === "\n" ? "a line break" : `'${ch}'`);
    const ws = () => { while (i < text.length && " \t\n\r".includes(text[i])) i++; };
    function value() {
      ws();
      const c = text[i];
      if (c === "{") return container("}", true);
      if (c === "[") return container("]", false);
      if (c === '"') return string();
      if (c === "-" || (c >= "0" && c <= "9")) return number();
      for (const word of ["true", "false", "null"]) if (text.startsWith(word, i) && !/\w/.test(text[i + word.length] || "")) { i += word.length; return; }
      if (/[A-Za-z_]/.test(c || "")) {
        const word = text.slice(i).match(/^[A-Za-z_]\w*/)[0];
        fail(`Unknown value '${word}': text goes in "double quotes"; true, false and null are written in lowercase`);
      }
      fail(c === undefined ? "The text ends before the value" : `Expected a value, found ${describe(c)}`);
    }
    function container(closer, isObject) {
      i++; ws();
      if (text[i] === closer) { i++; return; }
      for (;;) {
        ws();
        if (isObject) {
          if (text[i] === closer) fail(`Comma before '${closer}': remove the last comma`, text.lastIndexOf(",", i));
          if (text[i] !== '"') fail(`Expected a key in "double quotes", found ${describe(text[i])}`);
          string(); ws();
          if (text[i] !== ":") fail(`Expected ':' after the key, found ${describe(text[i])}`);
          i++;
        } else if (text[i] === closer) {
          fail(`Comma before '${closer}': remove the last comma`, text.lastIndexOf(",", i));
        }
        value(); ws();
        if (text[i] === ",") { i++; continue; }
        if (text[i] === closer) { i++; return; }
        const before = i;
        ws();
        fail(text[i] === '"' || text[i] === "{" || text[i] === "[" || /[\w-]/.test(text[i] || "")
          ? `A comma is missing before ${describe(text[i])}`
          : `Expected ',' or '${closer}', found ${describe(text[i])}`, text[i] === undefined ? before : i);
      }
    }
    function string() {
      const start = i;
      i++;
      while (i < text.length) {
        const c = text[i];
        if (c === '"') { i++; return; }
        if (c === "\\") {
          const n = text[i + 1];
          if (n === "u" && !/^[0-9a-fA-F]{4}$/.test(text.slice(i + 2, i + 6))) fail("Invalid \\u escape: 4 hexadecimal digits expected");
          if (!'"\\/bfnrtu'.includes(n ?? "")) fail(`Invalid escape '\\${n ?? ""}' (write \\\\ for a backslash)`);
          i += n === "u" ? 6 : 2;
          continue;
        }
        if (c === "\n") fail('Unclosed text: a " is missing at the end of the line');
        if (c < " ") fail("Control character in a text: use \\t or \\n");
        i++;
      }
      fail('Unclosed text: a " is missing', start);
    }
    function number() {
      const m = text.slice(i).match(/^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/);
      if (!m || m[0] === "-") fail("Invalid number");
      i += m[0].length;
      if (/[\w.]/.test(text[i] || "")) fail("Invalid number");
    }
    try {
      value(); ws();
      if (i < text.length) fail(`Unexpected ${describe(text[i])} after the end of the data`);
      return null;
    } catch (e) {
      if (e && typeof e.at === "number") return e;
      throw e;
    }
  }

  /* {line, col, message} (1-based) of the error that made JSON.parse fail. */
  function parseError(text, err) {
    const found = locateError(text);
    if (found) {
      const at = Math.max(0, Math.min(found.at, text.length));
      const before = text.slice(0, at).split("\n");
      return { line: before.length, col: before[before.length - 1].length + 1, message: found.message };
    }
    const lines = text.split("\n");                     // the browser refused something the scanner accepted
    return { line: lines.length, col: lines[lines.length - 1].length + 1, message: String(err.message || err) };
  }

  /* Lines changed since loading: common prefix/suffix, then an LCS on the middle part. */
  function changedLines(before, after) {
    const a = before.split("\n"), b = after.split("\n");
    let start = 0;
    while (start < a.length && start < b.length && a[start] === b[start]) start++;
    let endA = a.length, endB = b.length;
    while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
    const changed = new Set(), deletedBefore = new Set();
    const midA = a.slice(start, endA), midB = b.slice(start, endB);
    if (!midA.length && !midB.length) return { changed, deletedBefore };
    if (midA.length * midB.length > 250000) {          // too big for an LCS: mark the whole block
      for (let i = start; i < endB; i++) changed.add(i);
      if (!midB.length) deletedBefore.add(start);
      return { changed, deletedBefore };
    }
    const n = midA.length, m = midB.length, dp = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
    for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--)
      dp[i][j] = midA[i] === midB[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    let i = 0, j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && midA[i] === midB[j]) { i++; j++; }
      else if (j < m && (i >= n || dp[i][j + 1] >= dp[i + 1][j])) { changed.add(start + j); j++; }
      else { deletedBefore.add(start + j); i++; }
    }
    return { changed, deletedBefore };
  }

  function create({ value = "", label = "JSON", rows = 22, onChange, onSave } = {}) {
    let original = value;
    const root = el("div", "code-editor");
    root.style.setProperty("--rows", rows);
    const toolbar = el("div", "ce-toolbar");
    const body = el("div", "ce-body");
    const gutterWrap = el("div", "ce-gutter");
    gutterWrap.setAttribute("aria-hidden", "true");
    const gutter = el("div", "ce-gutter-inner");
    gutterWrap.append(gutter);
    const scroller = el("div", "ce-scroller");
    const layer = el("div", "ce-layer");
    layer.setAttribute("aria-hidden", "true");
    const lineMark = el("div", "ce-current");
    const code = el("pre", "ce-code");
    layer.append(lineMark, code);
    const area = el("textarea", "ce-input");
    area.spellcheck = false;
    area.setAttribute("autocapitalize", "off");
    area.setAttribute("autocomplete", "off");
    area.setAttribute("wrap", "off");
    area.setAttribute("aria-label", `${label} (JSON). Ctrl+S saves, Shift+Alt+F formats.`);
    area.value = value;
    scroller.append(layer, area);
    body.append(gutterWrap, scroller);
    const status = el("div", "ce-status");
    status.setAttribute("role", "status");
    const pos = el("span", "ce-pos"), diff = el("span", "ce-diff"), state = el("span", "ce-state");
    status.append(pos, diff, state);
    root.append(toolbar, body, status);

    let parsed, error = null, frame = 0;

    /* --------------------------------------------------------- rendering */
    function render() {
      frame = 0;
      const text = area.value;
      try { parsed = JSON.parse(text); error = null; }
      catch (err) { parsed = undefined; error = parseError(text, err); }
      const { changed, deletedBefore } = changedLines(original, text);
      const caret = area.selectionStart;
      const match = bracketPair(text, caret);
      const errorIndex = error ? indexOf(text, error.line, error.col) : -1;

      // Highlighted copy of the text.
      const frag = document.createDocumentFragment();
      const marks = new Map();
      if (match) { marks.set(match[0], "ce-match"); marks.set(match[1], "ce-match"); }
      let last = 0, m;
      TOKEN.lastIndex = 0;
      const push = (str, cls, at) => {
        if (!str) return;
        // Split the token around single-character marks (bracket match, error spot).
        let offset = 0;
        for (let k = 0; k < str.length; k++) {
          const markCls = at + k === errorIndex ? "ce-error-spot" : marks.get(at + k);
          if (!markCls) continue;
          if (k > offset) frag.append(cls ? el("span", cls, str.slice(offset, k)) : document.createTextNode(str.slice(offset, k)));
          frag.append(el("span", `${cls || ""} ${markCls}`.trim(), str[k] === "\n" ? " \n" : str[k]));
          offset = k + 1;
        }
        if (offset < str.length) frag.append(cls ? el("span", cls, str.slice(offset)) : document.createTextNode(str.slice(offset)));
      };
      while ((m = TOKEN.exec(text)) !== null) {
        if (m.index > last) push(text.slice(last, m.index), null, last);
        let cls = null;
        if (m[1] !== undefined) cls = /^\s*:/.test(text.slice(TOKEN.lastIndex, TOKEN.lastIndex + 40)) ? "ce-key" : "ce-string";
        else if (m[2] !== undefined) cls = "ce-number";
        else if (m[3] !== undefined) cls = "ce-literal";
        else if (m[4] !== undefined) cls = "ce-punct";
        else if (m[6] !== undefined) cls = "ce-bad";
        push(m[0], cls, m.index);
        last = TOKEN.lastIndex;
      }
      if (errorIndex >= text.length) frag.append(el("span", "ce-error-spot", " "));
      frag.append("\n");                                        // keeps the last empty line visible
      code.replaceChildren(frag);

      // Gutter: numbers, changed lines, deletions, error line.
      const count = text.split("\n").length;
      const gfrag = document.createDocumentFragment();
      for (let i = 0; i < count; i++) {
        const cell = el("div", "ce-num", String(i + 1));
        if (changed.has(i)) cell.classList.add("ce-changed");
        if (deletedBefore.has(i)) cell.classList.add("ce-deleted");
        if (error && error.line === i + 1) { cell.classList.add("ce-error-line"); cell.title = error.message; }
        gfrag.append(cell);
      }
      if (deletedBefore.has(count)) gfrag.lastChild.classList.add("ce-deleted-after");
      gutter.replaceChildren(gfrag);

      // Status bar.
      const before = text.slice(0, caret).split("\n");
      pos.textContent = `Line ${before.length}, column ${before[before.length - 1].length + 1}`;
      const n = changed.size + (changed.size === 0 && deletedBefore.size ? 1 : 0);
      diff.textContent = n ? `${n} line${n === 1 ? "" : "s"} changed` : "No change";
      state.className = `ce-state ${error ? "bad" : "ok"}`;
      state.textContent = error ? `Line ${error.line}, column ${error.col}: ${error.message}` : "Valid JSON";
      root.classList.toggle("ce-invalid", !!error);
      placeCurrentLine();
      sync();
      if (onChange) onChange(api);
    }
    const schedule = () => { if (!frame) frame = requestAnimationFrame(render); };

    function placeCurrentLine() {
      const lineNo = area.value.slice(0, area.selectionStart).split("\n").length - 1;
      lineMark.style.transform = `translateY(${lineNo * lineHeight()}px)`;
      lineMark.hidden = area.selectionStart !== area.selectionEnd;
    }
    function lineHeight() { return parseFloat(getComputedStyle(area).lineHeight) || 20; }
    function sync() {
      layer.style.transform = `translate(${-area.scrollLeft}px, ${-area.scrollTop}px)`;
      gutter.style.transform = `translateY(${-area.scrollTop}px)`;
    }

    function indexOf(text, line, col) {
      let index = 0;
      for (let l = 1; l < line; l++) { const nl = text.indexOf("\n", index); if (nl < 0) break; index = nl + 1; }
      return index + col - 1;
    }

    /* Bracket next to the caret and its partner, outside strings. */
    function bracketPair(text, caret) {
      const open = "{[", close = "}]";
      for (const at of [caret - 1, caret]) {
        const ch = text[at];
        if (!ch || !(open + close).includes(ch) || inString(text, at)) continue;
        const forward = open.includes(ch);
        const partner = forward ? close[open.indexOf(ch)] : open[close.indexOf(ch)];
        let depth = 0;
        for (let i = at; forward ? i < text.length : i >= 0; i += forward ? 1 : -1) {
          const c = text[i];
          if ((c === ch || c === partner) && !inString(text, i)) {
            depth += c === ch ? 1 : -1;
            if (depth === 0) return [at, i];
          }
        }
        return null;
      }
      return null;
    }
    function inString(text, at) {
      const start = text.lastIndexOf("\n", at - 1) + 1;
      let quotes = 0;
      for (let i = start; i < at; i++) { if (text[i] === "\\") { i++; continue; } if (text[i] === '"') quotes++; }
      return quotes % 2 === 1;
    }

    /* ----------------------------------------------------------- editing */
    // execCommand keeps the native undo history (Ctrl+Z); setRangeText is the fallback.
    function insert(text, selectStart, selectEnd) {
      area.focus();
      if (!document.execCommand || !document.execCommand("insertText", false, text)) {
        area.setRangeText(text, area.selectionStart, area.selectionEnd, "end");
        area.dispatchEvent(new Event("input"));
      }
      if (selectStart !== undefined) area.setSelectionRange(selectStart, selectEnd ?? selectStart);
    }
    function lineStart(text, i) { return text.lastIndexOf("\n", i - 1) + 1; }

    function onKey(e) {
      const text = area.value, s = area.selectionStart, t = area.selectionEnd;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key.toLowerCase() === "s") { e.preventDefault(); if (onSave) onSave(api); return; }
      if (e.shiftKey && e.altKey && e.key.toLowerCase() === "f") { e.preventDefault(); format(); return; }
      if (e.key === "Tab" || (mod && (e.key === "]" || e.key === "["))) {
        if (e.key === "Tab" && !e.shiftKey && s === t) { e.preventDefault(); insert(INDENT); return; }
        e.preventDefault();
        shiftLines(e.shiftKey || e.key === "[" ? -1 : 1);
        return;
      }
      if (e.key === "Enter" && !mod) {
        e.preventDefault();
        const ls = lineStart(text, s);
        const indent = text.slice(ls, s).match(/^\s*/)[0];
        const prev = text.slice(ls, s).trimEnd().slice(-1), next = text[t];
        if ((prev === "{" && next === "}") || (prev === "[" && next === "]")) {
          const inner = `\n${indent}${INDENT}`;
          insert(`${inner}\n${indent}`, s + inner.length);
        } else {
          insert(`\n${indent}${prev === "{" || prev === "[" ? INDENT : ""}`);
        }
        return;
      }
      if (e.key === "Backspace" && s === t && s > 0) {
        const pair = PAIRS[text[s - 1]];
        if (pair && text[s] === pair) { e.preventDefault(); area.setSelectionRange(s - 1, s + 1); insert(""); return; }
        const ls = lineStart(text, s);
        if (s > ls && /^ +$/.test(text.slice(ls, s)) && (s - ls) % INDENT.length === 0) {
          e.preventDefault(); area.setSelectionRange(s - INDENT.length, s); insert(""); return;
        }
      }
      if (!mod && !e.altKey && e.key.length === 1) {
        const quoted = inString(text, s);
        if (CLOSERS.has(e.key) && s === t && text[s] === e.key && (e.key === '"' ? quoted : !quoted)) {
          e.preventDefault(); area.setSelectionRange(s + 1, s + 1); schedule(); return;   // type over the closer
        }
        const close = PAIRS[e.key];
        if (close && !quoted) {
          e.preventDefault();
          if (s !== t) { insert(e.key + text.slice(s, t) + close, s + 1, t + 1); return; }  // wrap the selection
          if (/[\s,:}\]]/.test(text[s] ?? " ")) { insert(e.key + close, s + 1); return; }
          insert(e.key);
        }
      }
    }

    function shiftLines(direction) {
      const text = area.value, s = area.selectionStart, t = area.selectionEnd;
      const from = lineStart(text, s);
      const toNl = text.indexOf("\n", t > s && text[t - 1] === "\n" ? t - 1 : t);
      const to = toNl < 0 ? text.length : toNl;
      const lines = text.slice(from, to).split("\n");
      const out = lines.map((l) => (direction > 0 ? INDENT + l : l.replace(new RegExp(`^ {1,${INDENT.length}}`), "")));
      area.setSelectionRange(from, to);
      insert(out.join("\n"), from, from + out.join("\n").length);
    }

    function format() {
      try {
        const formatted = JSON.stringify(JSON.parse(area.value), null, 2);
        if (formatted === area.value) return true;
        area.setSelectionRange(0, area.value.length);
        insert(formatted, 0);
        return true;
      } catch {
        schedule();
        return false;
      }
    }

    function revert() {
      area.setSelectionRange(0, area.value.length);
      insert(original, 0);
    }

    area.addEventListener("input", schedule);
    area.addEventListener("keydown", onKey);
    area.addEventListener("scroll", sync);
    for (const ev of ["click", "keyup", "select", "focus"]) area.addEventListener(ev, () => { placeCurrentLine(); schedule(); });

    const api = {
      element: root,
      toolbar,
      textarea: area,
      get value() { return area.value; },
      /* The parsed value, or undefined if the JSON is invalid. */
      get parsed() { return error ? undefined : parsed; },
      get error() { return error; },
      get dirty() { return area.value !== original; },
      format,
      revert,
      /* Replaces the content and makes it the new reference for "changed lines". */
      reset(text) { original = text; area.value = text; render(); },
      focus() { area.focus(); },
      /* Puts the caret on the error, for a Save that refused invalid JSON. */
      showError() {
        if (!error) return;
        const i = indexOf(area.value, error.line, error.col);
        area.focus();
        area.setSelectionRange(i, i);
        const lh = lineHeight();
        area.scrollTop = Math.max(0, (error.line - 4) * lh);
        schedule();
      },
    };
    render();
    return api;
  }

  return { create, parseError, locateError, changedLines };
})();
