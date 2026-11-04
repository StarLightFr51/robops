"use strict";
/* Small SVG charts for the robops dashboard: time lines and columns (plain or stacked).
   No external library. Marks follow fixed specs: 2px lines, columns <= 24px with a 4px
   rounded cap and a square base, a 2px surface gap between touching marks, hairline
   solid grid, one y-axis. Every chart has a crosshair or per-column tooltip (mouse and
   arrow keys) and a table view. Labels are inserted with textContent only. */

const RobopsCharts = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const PLOT_H = 190, AXIS_H = 26, PAD_L = 48, PAD_R = 56, PAD_T = 14;

  function svgEl(tag, attrs = {}, text) {
    const el = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, v);
    if (text !== undefined) el.textContent = text;
    return el;
  }
  function htmlEl(tag, cls, text) {
    const el = document.createElement(tag);
    if (cls) el.className = cls;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  const full = new Intl.NumberFormat();
  const short = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
  function number(v, max = v) { return Math.abs(max) >= 10000 ? short.format(v) : full.format(Math.round(v * 10) / 10); }

  /* Clean ticks 0..max (or min..max) with ~4 steps of 1/2/2.5/5 x 10^n. */
  function ticks(lo, hi, count = 4) {
    if (hi <= lo) hi = lo + 1;
    const raw = (hi - lo) / count, exp = Math.floor(Math.log10(raw)), base = 10 ** exp;
    const step = [1, 2, 2.5, 5, 10].map((m) => m * base).find((s) => s >= raw);
    const start = Math.floor(lo / step) * step, out = [];
    for (let v = start; v < hi + step * 0.999; v += step) out.push(Math.round(v * 1e6) / 1e6);
    return out;
  }

  /* Rounded cap (4px) on top, square at the baseline. */
  function columnPath(x, y, w, h, r = 4) {
    if (h <= 0) return "";
    r = Math.min(r, w / 2, h);
    return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
  }

  function frame(container, spec) {
    container.classList.add("chart");
    const width = Math.max(260, container.clientWidth || 600);
    const svg = svgEl("svg", { width, height: PLOT_H + AXIS_H + PAD_T, role: "img", tabindex: "0",
      "aria-label": spec.label, class: "chart-svg" });
    const tip = htmlEl("div", "chart-tip");
    tip.hidden = true;
    container.replaceChildren(svg, tip);
    return { svg, tip, width, plotW: width - PAD_L - PAD_R };
  }

  function yAxis(svg, width, scaleTicks, y, format) {
    const max = Math.max(...scaleTicks.map(Math.abs));
    for (const t of scaleTicks) {
      const yy = y(t);
      svg.append(svgEl("line", { x1: PAD_L, x2: width - PAD_R, y1: yy, y2: yy, class: t === scaleTicks[0] ? "chart-base" : "chart-grid" }));
      svg.append(svgEl("text", { x: PAD_L - 8, y: yy + 4, class: "chart-tick", "text-anchor": "end" }, format ? format(t, max) : number(t, max)));
    }
  }

  function showTip(state, x, lines) {
    const { tip, svg } = state;
    tip.replaceChildren(...lines.map(([value, label], i) => {
      const row = htmlEl("div", i ? "chart-tip-row" : "chart-tip-row first");
      row.append(htmlEl("strong", null, value), htmlEl("span", null, label));
      return row;
    }));
    tip.hidden = false;
    // Beside the crosshair (flipped to the left near the right edge), never on top of the axis labels.
    const w = tip.offsetWidth;
    const left = x + 14 + w > svg.clientWidth ? x - 14 - w : x + 14;
    tip.style.left = `${Math.max(0, left)}px`;
    tip.style.top = `${PAD_T}px`;
  }

  function keyboard(svg, count, move) {
    let index = count - 1;
    svg.addEventListener("focus", () => move(index));
    svg.addEventListener("blur", () => move(-1));
    svg.addEventListener("keydown", (e) => {
      if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "Home" || e.key === "End") {
        e.preventDefault();
        index = e.key === "Home" ? 0 : e.key === "End" ? count - 1 : Math.max(0, Math.min(count - 1, index + (e.key === "ArrowRight" ? 1 : -1)));
        move(index);
      }
    });
  }

  /* ------------------------------------------------------------ time line */
  /* spec: { label, points: [{t: ms, v}], from, to, format(v), date(ms), gap: ms, zero: bool } */
  function line(container, spec) {
    const st = frame(container, spec);
    const { svg, width, plotW } = st;
    const pts = spec.points.filter((p) => typeof p.v === "number");
    const from = spec.from ?? pts[0].t, to = spec.to ?? pts[pts.length - 1].t;
    const values = pts.map((p) => p.v);
    let lo = spec.zero === false ? Math.min(...values) : 0, hi = Math.max(...values, spec.zero === false ? lo + 1 : 1);
    if (spec.zero === false) { const pad = (hi - lo) * 0.15 || 1; lo = Math.max(spec.min ?? -Infinity, lo - pad); hi = Math.min(spec.max ?? Infinity, hi + pad); }
    const yt = ticks(lo, hi);
    const y = (v) => PAD_T + PLOT_H - ((v - yt[0]) / (yt[yt.length - 1] - yt[0])) * PLOT_H;
    const x = (t) => PAD_L + ((t - from) / (to - from || 1)) * plotW;
    yAxis(svg, width, yt, y, spec.format);

    // X ticks: hours for short spans, days otherwise.
    const span = to - from;
    const fmt = new Intl.DateTimeFormat(undefined, span <= 36 * 3600e3 ? { hour: "2-digit", minute: "2-digit" } : { day: "numeric", month: "short" });
    const n = Math.max(2, Math.min(6, Math.floor(plotW / 110)));
    for (let i = 0; i <= n; i++) {
      const t = from + (span * i) / n;
      svg.append(svgEl("text", { x: x(t), y: PAD_T + PLOT_H + 18, class: "chart-tick", "text-anchor": i === 0 ? "start" : i === n ? "end" : "middle" }, fmt.format(t)));
    }

    // Segments: the line breaks where samples are missing (dashboard closed) instead of inventing values.
    const segments = [];
    for (const p of pts) {
      const last = segments[segments.length - 1];
      if (last && p.t - last[last.length - 1].t <= (spec.gap ?? Infinity)) last.push(p);
      else segments.push([p]);
    }
    for (const seg of segments) {
      const d = seg.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
      if (seg.length > 1) {
        svg.append(svgEl("path", { d: `${d}L${x(seg[seg.length - 1].t).toFixed(1)},${y(yt[0])}L${x(seg[0].t).toFixed(1)},${y(yt[0])}Z`, class: "chart-area" }));
        svg.append(svgEl("path", { d, class: "chart-line" }));
      } else {
        svg.append(svgEl("circle", { cx: x(seg[0].t), cy: y(seg[0].v), r: 2.5, class: "chart-point" }));
      }
    }
    // End dot with surface ring + the last value as the one direct label.
    const end = pts[pts.length - 1];
    svg.append(svgEl("circle", { cx: x(end.t), cy: y(end.v), r: 4, class: "chart-dot" }));
    svg.append(svgEl("text", { x: x(end.t) + 8, y: y(end.v) + 4, class: "chart-label" }, spec.format ? spec.format(end.v) : number(end.v)));

    // Crosshair: snaps to the nearest sample.
    const cross = svgEl("line", { y1: PAD_T, y2: PAD_T + PLOT_H, class: "chart-cross", visibility: "hidden" });
    const focus = svgEl("circle", { r: 4, class: "chart-dot", visibility: "hidden" });
    svg.append(cross, focus);
    const date = spec.date || ((t) => new Date(t).toLocaleString());
    const move = (i) => {
      if (i < 0) { cross.setAttribute("visibility", "hidden"); focus.setAttribute("visibility", "hidden"); st.tip.hidden = true; return; }
      const p = pts[i], px = x(p.t);
      for (const el of [cross]) { el.setAttribute("x1", px); el.setAttribute("x2", px); el.setAttribute("visibility", "visible"); }
      focus.setAttribute("cx", px); focus.setAttribute("cy", y(p.v)); focus.setAttribute("visibility", "visible");
      showTip(st, px, [[spec.format ? spec.format(p.v) : number(p.v), date(p.t)]]);
    };
    const hit = svgEl("rect", { x: PAD_L, y: PAD_T, width: plotW, height: PLOT_H, class: "chart-hit" });
    hit.addEventListener("pointermove", (e) => {
      const box = svg.getBoundingClientRect(), t = from + ((e.clientX - box.left - PAD_L) / plotW) * (to - from);
      let best = 0;
      for (let i = 1; i < pts.length; i++) if (Math.abs(pts[i].t - t) < Math.abs(pts[best].t - t)) best = i;
      move(best);
    });
    hit.addEventListener("pointerleave", () => move(-1));
    svg.append(hit);
    keyboard(svg, pts.length, move);
  }

  /* -------------------------------------------------------------- columns */
  /* spec: { label, bars: [{label, value | parts:{key:n}, tip}], series: [{key, label, cls}], format(v), labelMax } */
  function columns(container, spec) {
    const st = frame(container, spec);
    const { svg, width, plotW } = st;
    const series = spec.series || [{ key: "value", label: spec.label, cls: "chart-bar" }];
    const total = (b) => (b.parts ? series.reduce((n, s) => n + (b.parts[s.key] || 0), 0) : b.value || 0);
    const hi = Math.max(1, ...spec.bars.map(total));
    const yt = ticks(0, hi);
    const top = yt[yt.length - 1];
    const y = (v) => PAD_T + PLOT_H - (v / top) * PLOT_H;
    yAxis(svg, width, yt, y, spec.format);
    const slot = plotW / spec.bars.length;
    const bw = Math.max(2, Math.min(24, slot - 2));            // 2px surface gap between touching columns
    const every = Math.max(1, Math.ceil((Math.max(...spec.bars.map((b) => String(b.label).length)) * 7 + 10) / slot));
    const fmt = spec.format || ((v) => number(v, hi));
    let maxIndex = 0;
    spec.bars.forEach((b, i) => { if (total(b) > total(spec.bars[maxIndex])) maxIndex = i; });

    spec.bars.forEach((b, i) => {
      const cx = PAD_L + slot * i + slot / 2, x0 = cx - bw / 2;
      let base = y(0);
      const visible = series.filter((s) => (b.parts ? b.parts[s.key] : b.value) > 0);
      visible.forEach((s, k) => {
        const v = b.parts ? b.parts[s.key] : b.value;
        const h = (v / top) * PLOT_H;
        const isTop = k === visible.length - 1;
        const gap = k > 0 ? 2 : 0;                              // surface gap between stacked segments
        const y0 = base - h;
        svg.append(svgEl("path", { d: isTop ? columnPath(x0, y0, bw, h - gap) : `M${x0},${y0}h${bw}v${h - gap}h${-bw}Z`, class: s.cls }));
        base = y0;
      });
      if ((i % every === 0 || i === spec.bars.length - 1) && (i === spec.bars.length - 1 || spec.bars.length - 1 - i >= every)) {
        svg.append(svgEl("text", { x: cx, y: PAD_T + PLOT_H + 18, class: "chart-tick", "text-anchor": "middle" }, b.label));
      }
    });
    // One direct label: the highest column.
    if (spec.labelMax !== false && total(spec.bars[maxIndex]) > 0) {
      const cx = PAD_L + slot * maxIndex + slot / 2;
      svg.append(svgEl("text", { x: cx, y: y(total(spec.bars[maxIndex])) - 6, class: "chart-label", "text-anchor": "middle" }, fmt(total(spec.bars[maxIndex]))));
    }

    const lift = svgEl("rect", { y: PAD_T, height: PLOT_H, class: "chart-lift", visibility: "hidden" });
    svg.insertBefore(lift, svg.firstChild);
    const move = (i) => {
      if (i < 0) { lift.setAttribute("visibility", "hidden"); st.tip.hidden = true; return; }
      const b = spec.bars[i], cx = PAD_L + slot * i + slot / 2;
      lift.setAttribute("x", cx - slot / 2); lift.setAttribute("width", slot); lift.setAttribute("visibility", "visible");
      const lines = b.parts
        ? [[fmt(total(b)), b.tip || b.label], ...series.filter((s) => b.parts[s.key]).map((s) => [fmt(b.parts[s.key]), s.label])]
        : [[fmt(b.value || 0), b.tip || b.label]];
      showTip(st, cx, lines);
    };
    spec.bars.forEach((b, i) => {
      const hit = svgEl("rect", { x: PAD_L + slot * i, y: PAD_T, width: slot, height: PLOT_H, class: "chart-hit" });
      hit.addEventListener("pointerenter", () => move(i));
      hit.addEventListener("pointerleave", () => move(-1));
      svg.append(hit);
    });
    keyboard(svg, spec.bars.length, move);
  }

  /* Legend (only for 2+ series): swatch shape mirrors the mark. */
  function legend(series) {
    const box = htmlEl("div", "chart-legend");
    for (const s of series) {
      const item = htmlEl("span", "chart-legend-item");
      const sw = svgEl("svg", { width: 12, height: 12, "aria-hidden": "true" });
      sw.append(svgEl("rect", { width: 12, height: 12, rx: 2, class: s.cls }));
      item.append(sw, document.createTextNode(s.label));
      box.append(item);
    }
    return box;
  }

  /* Table twin of a chart: every value readable without hovering. */
  function table(headers, rows) {
    const details = htmlEl("details", "chart-table");
    details.append(htmlEl("summary", null, "Show as a table"));
    const t = htmlEl("table");
    const head = htmlEl("tr");
    headers.forEach((hd, i) => head.append(htmlEl("th", i ? "right" : null, hd)));
    t.append(htmlEl("thead"));
    t.tHead.append(head);
    const body = htmlEl("tbody");
    for (const row of rows) {
      const tr = htmlEl("tr");
      row.forEach((cell, i) => tr.append(htmlEl("td", i ? "right num" : null, String(cell))));
      body.append(tr);
    }
    t.append(body);
    details.append(t);
    return details;
  }

  /* Renders now, and again when the card changes width. */
  function mount(container, draw) {
    draw();
    let last = container.clientWidth;
    const ro = new ResizeObserver(() => {
      if (Math.abs(container.clientWidth - last) < 4) return;
      last = container.clientWidth;
      requestAnimationFrame(draw);
    });
    ro.observe(container);
  }

  return { line, columns, legend, table, mount, number };
})();
