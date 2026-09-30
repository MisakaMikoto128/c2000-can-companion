/* CAN Companion — 变量观察（WATCH）视图
 * 选择符号文件 → 自动校验（0x35 单帧 CRC，旧固件自动回退逐段读回）→ 加变量 →
 * 周期观察 → 行内改值写入。表格用 Tabulator 6.6.0（本地内置，主题映射见
 * watch.css）；表达式行用 expr-eval 2.0.2（本地内置）；
 * 数据来自后端推送 onPush(d.w)（watch.py 的 WatchManager.snapshot）。 */

/* 图表配色随主题：主题插件的 chart 配色（dark/light.js 注册），缺失时暗黑兜底 */
function currentChartTheme() {
  const fallback = { v: "#35F0A0", i: "#3EC6E0", tick: "#7C8A99", grid: "#1A212B",
                     border: "#232B36", font: "Cascadia Mono, Consolas, monospace" };
  return (window.ThemeRegistry && ThemeRegistry.chartColors()) || fallback;
}

const watch = {
  inited: false,
  table: null,
  st: null,          // 最近一次推送的快照
  editingId: null,   // 正在行内编辑的行（推送不覆盖它）
  toastSeq: 0,       // 已展示的后端一次性提醒序号（升级完成提醒只弹一次）
  restored: false,   // 页面刷新后已拉过整表重建
  symbols: [],       // 当前符号表的变量名（datalist 合并用）
  exprParser: null,  // expr-eval Parser（表达式行共用）
  exprSeq: 0,        // 表达式行计数（行 id 前缀 x）
  exprs: [],         // 表达式行 [{id, text, expr, back, last}]
  enumVals: {},      // 行 id -> 枚举成员名列表（树子行组件与 getRow 取数不同步，编辑器按 id 查这里）
  stats: {},         // 行 id -> {min, max}（数值行极值，含表达式行；会话内累计）
  wave: { pins: [], chart: null, timer: null },  // 波形通道与面板
};

const WATCH_NAME_KEY = "ccWatchNames";   // 最近成功添加的变量名（最多 8 条，重启保留）
const WATCH_DIR_KEY = "ccWatchDir";      // 上次选择符号文件的文件夹（对话框初始目录）
const WATCH_NAME_MAX = 8;
const WATCH_SESSION_KEY = "ccWatchSession"; // 观察会话（文件+地址+列表+选项+表达式）

/* ---------- 表格 ---------- */
function watchInitTable() {
  if (watch.table) return;
  watch.table = new Tabulator("#watchTable", {
    index: "id",
    dataTree: true,
    dataTreeStartExpanded: true,
    layout: "fitColumns",
    placeholder: "校验通过后，在这里添加要观察的变量",
    cellEditing: (cell) => { watch.editingId = cell.getRow().getData().id; },
    cellEditCancelled: () => { watch.editingId = null; },
    rowFormatter: (row) => {
      const d = row.getData();
      row.getElement().classList.toggle("watch-chg", !!d.changed);
      row.getElement().classList.toggle("watch-missing", !!d.missing);
    },
    // Tabulator 6.6 的行右键菜单选项名是 rowContextMenu（rowContext 只是事件转发）
    rowContextMenu: (e, row) => {
      const isExpr = String(row.getData().id).startsWith("x");
      const items = [{
        label: isExpr ? "复制表达式" : "复制变量名",
        action: (e, row) => watchCopyRow(row),
      }];
      if (!isExpr) {
        const pinned = watch.wave.pins.indexOf(String(row.getData().id)) >= 0;
        items.push({ label: pinned ? "移出波形" : "加入波形",
                     action: (e, row) => watchWaveToggle(row) });
      }
      items.push({ label: "清零统计", action: (e, row) => watchStatReset(row) });
      items.push({ label: isExpr ? "移除该表达式" : "移除该变量",
                   action: (e, row) => watchRemoveRow(row) });
      return items;
    },
    columns: [
      { title: "名称", field: "name", minWidth: 200,
        formatter: (cell) => {
          const el = document.createElement("span");
          el.textContent = cell.getValue();
          const d = cell.getData();
          if (d.missing) el.title = "当前符号表里没有该变量，轮询已跳过";
          else if (!d.writable) el.title = "Flash 区变量，只读";
          return el;
        } },
      { title: "值", field: "val", width: 140, hozAlign: "right",
        formatter: (cell) => {
          const el = document.createElement("span");
          el.textContent = cell.getValue();
          const raw = cell.getData().raw;
          if (raw) el.title = raw;   // 浮点悬停全精度（列内按 %g 六位有效数字显示）
          return el;
        },
        editable: (cell) => {
          const d = cell.getRow().getData();
          return !!d.editable && !!d.writable && !!watch.st && watch.st.st === "ready";
        },
        editor: (cell, onRendered, success, cancel, editorParams) => {
          const d = cell.getRow().getData();
          // 编辑器一打开就登记：推送跳过本行，编辑不被轮询刷新打断
          // （cellEditing 回调实测不触发，不能依赖它置位）
          watch.editingId = d.id;
          const ev = watch.enumVals[d.id] || d.enumVals;
          if (ev && ev.length) {
            // 枚举：自绘下拉选择（可自由输入数值），提交枚举名由后端换算。
            // 不用原生 datalist：其候选按输入值过滤，预填的解码值「名 (数值)」
            // 会把全部候选滤空，点箭头无列表
            const inp = document.createElement("input");
            inp.value = cell.getValue();
            inp.style.flex = "1";
            inp.style.minWidth = "0";
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "watch-enum-btn";
            btn.textContent = "▼";
            btn.title = "展开枚举成员";
            const wrap = document.createElement("div");
            wrap.style.cssText = "display:flex;align-items:center;width:100%;";
            wrap.appendChild(inp);
            wrap.appendChild(btn);
            const dd = document.createElement("div");
            dd.className = "watch-enum-dd";
            for (const v of ev) {
              const o = document.createElement("div");
              o.className = "watch-enum-opt";
              o.textContent = v;
              // mousedown 即选中：preventDefault 保住输入框焦点，blur 提交不会抢先触发
              o.addEventListener("mousedown", (e) => {
                e.preventDefault();
                inp.value = v;
                done(true);
              });
              dd.appendChild(o);
            }
            if (watch.enumDd) watch.enumDd.remove();   // 兜底清上一编辑器的浮层
            watch.enumDd = dd;
            let open = false;
            const close = () => { dd.remove(); open = false; };
            const show = () => {
              const r = inp.getBoundingClientRect();
              dd.style.width = Math.max(r.width, 140) + "px";
              // 下方放不下且上方放得下时向上展开
              dd.style.top = (window.innerHeight - r.bottom < 170 && r.top > 170)
                ? (r.top - 170) + "px" : r.bottom + "px";
              dd.style.left = r.left + "px";
              document.body.appendChild(dd);
              open = true;
            };
            btn.addEventListener("mousedown", (e) => {
              e.preventDefault();   // 不夺走输入框焦点，blur 不会提前提交
              open ? close() : show();
            });
            const done = (ok) => {
              // 同值提交 Tabulator 不触发 cellEdited，复位必须放在编辑器关闭处
              watch.editingId = null;
              close();
              if (watch.enumDd === dd) watch.enumDd = null;
              ok ? success(inp.value) : cancel();
            };
            inp.addEventListener("keydown", (e) => {
              if (e.key === "Enter") done(true);
              if (e.key === "Escape") done(false);
            });
            inp.addEventListener("blur", () => done(true));
            onRendered(() => { inp.focus(); });
            return wrap;
          }
          const inp = document.createElement("input");
          inp.value = cell.getValue();
          inp.style.width = "100%";
          onRendered(() => { inp.focus(); inp.select(); });
          const done = (ok) => {
            // 同值提交 Tabulator 不触发 cellEdited，复位必须放在编辑器关闭处
            watch.editingId = null;
            ok ? success(inp.value) : cancel();
          };
          inp.addEventListener("keydown", (e) => {
            if (e.key === "Enter") done(true);
            if (e.key === "Escape") done(false);
          });
          inp.addEventListener("blur", () => done(true));
          return inp;
        },
        cellEdited: (cell) => {
          watch.editingId = null;   // 编辑结束恢复该行的推送刷新
          const d = cell.getRow().getData();
          pywebview.api.watch_write(d.id, String(cell.getValue()))
            .then((r) => { if (r && r.success === false) showToast("error", r.message); });
        } },
      { title: "极值", field: "minmax", width: 130, hozAlign: "right" },
      { title: "地址", field: "addr", width: 90 },
      { title: "类型", field: "tname", minWidth: 110 },
      { title: "字数", field: "words", width: 60, hozAlign: "right" },
      { title: "状态", field: "err", minWidth: 120,
        formatter: (cell) => {
          const el = document.createElement("span");
          const d = cell.getData();
          el.textContent = d.missing ? "符号缺失" : (cell.getValue() || "");
          if (el.textContent) el.style.color = d.missing ? "var(--amber)" : "var(--red)";
          return el;
        } },
    ],
  });
  watch.inited = true;
}

/* ---------- 推送入口（app.js onPush 转发） ---------- */
function watchOnPush(w) {
  watchRestoreTable();   // 首次进入视图拉整表（含 idle 态未解析条目）
  if (!w) return;        // idle 态快照为 null，无值补丁可应用
  watch.st = w;
  if (w.toast && w.toast_seq && w.toast_seq !== watch.toastSeq) {
    watch.toastSeq = w.toast_seq;
    showToast("warning", w.toast);
  }
  watchRenderState(w);
  if (w.rows && w.rows.length && watch.table) {
    watchRegisterEnums(w.rows);
    const patch = {};
    for (const r of w.rows) {
      if (r.id === watch.editingId) continue;
      watchStatUpdate(r.id, r.val);
      const o = { changed: !!r.ch, err: r.err || "",
                  raw: r.raw || "", minmax: watchStatText(r.id) };
      if (r.val !== null && r.val !== undefined) o.val = r.val;
      patch[r.id] = o;
    }
    // updateData 只匹配顶层行（树形子行匹配不到），必须按行组件递归 update
    watchWalkRows(watch.table.getRows(), (row) => {
      const p = patch[row.getData().id];
      if (!p) return;
      row.update(p);
      row.getElement().classList.toggle("watch-chg", !!p.changed);
    });
  }
  watchEvalExprs();
}

/* ---------- 行统计（min/max） ----------
 * 数值行自加入（或上次清零）起累计极值，随推送刷新进「极值」列；口径与
 * 表达式行代入一致：parseFloat 取不到数则不计（枚举「名 (值)」、布尔、
 * 指针不参与）。表达式行在求值处同样累计。 */
function watchStatUpdate(id, val) {
  const v = parseFloat(val);
  if (Number.isNaN(v)) return;
  const s = watch.stats[id];
  if (s) {
    if (v < s.min) s.min = v;
    if (v > s.max) s.max = v;
  } else {
    watch.stats[id] = { min: v, max: v };
  }
}

function watchStatText(id) {
  const s = watch.stats[id];
  if (!s) return "";
  const f = (n) => String(Number(n.toPrecision(6)));
  return f(s.min) + " ~ " + f(s.max);
}

/* 清零统计：该行及其子行的极值重新累计（表达式行只清自身） */
function watchStatReset(row) {
  const id = String(row.getData().id);
  const hit = {};
  for (const k of Object.keys(watch.stats)) {
    if (k === id || k.startsWith(id + ".") || k.startsWith(id + "[")) {
      delete watch.stats[k];
      hit[k] = true;
    }
  }
  if (!watch.table) return;
  watchWalkRows(watch.table.getRows(), (r) => {
    if (hit[r.getData().id]) r.update({ minmax: "" });
  });
}

/* ---------- 波形（轮询值的趋势图） ----------
 * 通道 = 观察行（右键「加入波形/移出波形」），后端随轮询 tick 记录数值行
 * 进环形缓冲；面板 500 ms 拉一次全量重绘；非数值（枚举/布尔/指针）与
 * 表达式行不参与。通道随变量移除自动剔除。 */
function watchWaveColors(n) {
  const c = currentChartTheme();
  const css = (name) => (getComputedStyle(document.documentElement)
    .getPropertyValue(name) || "").trim();
  const base = [c.v, c.i, css("--amber"), css("--accent"), css("--red"), css("--muted")];
  return base[n % base.length] || c.v;
}

function watchWaveToggle(row) {
  const id = String(row.getData().id);
  const i = watch.wave.pins.indexOf(id);
  if (i >= 0) watch.wave.pins.splice(i, 1);
  else watch.wave.pins.push(id);
  pywebview.api.watch_wave_pins(watch.wave.pins.slice());
  watchWavePanel(true);
  watchWaveRebuild();
  watchWavePull();
}

function watchWavePanelToggle() {
  watchWavePanel($("watchWave").style.display === "none");
}

function watchWavePanel(show) {
  $("watchWave").style.display = show ? "block" : "none";
  if (show) {
    watchWaveRebuild();
    watchWavePull();
    if (!watch.wave.timer) {
      watch.wave.timer = setInterval(watchWavePull, 500);
    }
  } else if (watch.wave.timer) {
    clearInterval(watch.wave.timer);
    watch.wave.timer = null;
  }
}

function watchWaveRebuild() {
  if (watch.wave.chart) {
    watch.wave.chart.destroy();
    watch.wave.chart = null;
  }
  if (!$("watchWave") || $("watchWave").style.display === "none") return;
  const c = currentChartTheme();
  watch.wave.chart = new Chart($("watchWaveCanvas"), {
    type: "line",
    data: { datasets: watch.wave.pins.map((rid, i) => ({
      label: rid, _rid: rid, data: [],
      borderColor: watchWaveColors(i), borderWidth: 1.5,
      pointRadius: 0, tension: 0.15,
    })) },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false,
      parsing: false, normalized: true, spanGaps: true,
      plugins: {
        legend: { display: watch.wave.pins.length > 1,
                  labels: { color: c.tick, font: { family: c.font },
                            boxWidth: 12 } },
        tooltip: { enabled: true, mode: "index", intersect: false,
                   backgroundColor: c.grid, titleColor: c.tick,
                   bodyColor: c.tick, borderColor: c.border,
                   titleFont: { family: c.font }, bodyFont: { family: c.font } },
      },
      scales: {
        x: { type: "linear", title: { display: true, text: "时间 s",
              color: c.tick, font: { family: c.font } },
             ticks: { color: c.tick, font: { family: c.font } },
             grid: { color: c.grid, borderDash: c.gridDash || [] },
             border: { color: c.border } },
        y: { ticks: { color: c.tick, font: { family: c.font } },
             grid: { color: c.grid, borderDash: c.gridDash || [] },
             border: { color: c.border } },
      },
    },
  });
}

function watchWaveRecolor() {
  if (!watch.wave.chart) return;
  const c = currentChartTheme();
  watch.wave.chart.data.datasets.forEach((ds, i) => {
    ds.borderColor = watchWaveColors(i);
  });
  const x = watch.wave.chart.options.scales.x;
  const y = watch.wave.chart.options.scales.y;
  x.ticks.color = c.tick; x.title.color = c.tick;
  x.grid.color = c.grid; x.grid.borderDash = c.gridDash || [];
  x.border.color = c.border;
  y.ticks.color = c.tick;
  y.grid.color = c.grid; y.grid.borderDash = c.gridDash || [];
  y.border.color = c.border;
  const tp = watch.wave.chart.options.plugins.tooltip;
  tp.backgroundColor = c.grid; tp.titleColor = c.tick; tp.bodyColor = c.tick;
  tp.borderColor = c.border;
  const lg = watch.wave.chart.options.plugins.legend.labels;
  lg.color = c.tick; lg.font.family = c.font;
  watch.wave.chart.update("none");
}

async function watchWavePull() {
  if (!watch.wave.chart || !watch.wave.pins.length) return;
  let d = null;
  try { d = await pywebview.api.watch_wave_data(); } catch (e) { return; }
  if (!d) return;
  // 后端已剔除随变量移除的通道：通道列表与之同步，数据集不一致则重建
  watch.wave.pins = watch.wave.pins.filter((rid) => d[rid] !== undefined);
  const datasets = watch.wave.chart.data.datasets;
  if (datasets.length !== watch.wave.pins.length) {
    watchWaveRebuild();
    return;
  }
  for (const ds of datasets) {
    const ch = d[ds._rid];
    ds.data = ch ? ch.pts.map((p) => ({ x: p[0], y: p[1] })) : [];
    if (ch) ds.label = ch.name;
  }
  let n = 0, t0 = Infinity, t1 = -Infinity;
  for (const ds of datasets) {
    n = Math.max(n, ds.data.length);
    if (ds.data.length) {
      t0 = Math.min(t0, ds.data[0].x);
      t1 = Math.max(t1, ds.data[ds.data.length - 1].x);
    }
  }
  $("watchWaveInfo").textContent = n
    ? "通道 " + datasets.length + " · " + n + " 点 · 窗口 " + (t1 - t0).toFixed(1) + " s"
    : "";
  watch.wave.chart.update("none");
}

async function watchWaveExport() {
  let d = null;
  try { d = await pywebview.api.watch_wave_data(); } catch (e) { return; }
  if (!d || !Object.keys(d).length) {
    showToast("warning", "没有波形数据");
    return;
  }
  const lines = ["time_s,name,value"];
  for (const rid of Object.keys(d)) {
    for (const p of d[rid].pts) {
      lines.push(p[0] + "," + d[rid].name + "," + p[1]);
    }
  }
  const r = await pywebview.api.watch_wave_export(lines.join("\r\n"), "watch_wave.csv");
  if (r && r.success) showToast("", "已导出 " + r.path);
  else if (r && r.message !== "已取消") showToast("error", r.message);
}

function watchWaveClear() {
  pywebview.api.watch_wave_clear();
  watchWavePull();
}

function watchWalkRows(rows, cb) {
  for (const row of rows) {
    cb(row);
    const kids = (row.getTreeChildren && row.getTreeChildren()) || [];
    if (kids.length) watchWalkRows(kids, cb);
  }
}

/* 页面刷新后快照只有值补丁、没有行结构：首次进入视图拉一次整表重建
 * （含 idle 态的未解析条目）。值列由后续推送补齐；只做一次，之后的
 * 清空/重映射仍由各操作自己负责。 */
function watchRestoreTable() {
  if (watch.restored || !watch.table) return;
  watch.restored = true;
  pywebview.api.watch_table().then((r) => {
    if (r && r.success && r.table && r.table.length && watch.table) {
      watch.table.replaceData(r.table.concat(watch.exprs.map(watchExprRowData)));
      watchRegisterEnums(r.table);
    }
  }).catch(() => { /* 窗口关闭中 */ });
}

/* 递归登记枚举行：编辑器按行 id 从这里取成员名（行组件快照不可靠） */
function watchRegisterEnums(rows) {
  for (const r of rows || []) {
    if (r.enumVals && r.enumVals.length) watch.enumVals[r.id] = r.enumVals;
    if (r._children) watchRegisterEnums(r._children);
  }
}

/* 状态行与控件使能；输入框的值不覆盖用户正在编辑的内容 */
function watchRenderState(w) {
  const line = $("watchStateLine");
  if (line) {
    line.textContent = (w.st === "verifying" && w.prog >= 0)
      ? "固件校验中 " + w.prog + "%" : w.msg;
    line.style.color = (w.st === "mismatch") ? "var(--red)"
      : (w.st === "ready") ? "var(--accent)" : "var(--muted)";
  }
  const conn = $("watchConn");
  if (conn) conn.textContent = w.addr ? "目标 " + w.addr : "";

  const hasFile = w.st !== "idle";
  $("watchFileChip").style.display = hasFile ? "flex" : "none";
  $("watchDrop").style.display = hasFile ? "none" : "block";
  if (hasFile && w.file) {
    $("watchFwName").textContent = w.file;
    $("watchFwMeta").textContent =
      w.fmt + " · " + w.nsym + " 符号" + (w.addr ? " · 目标 " + w.addr : "");
  }
  // 清单编辑（添加/表达式/清空）独立于会话状态，随时可用；
  // 波形与轮询仍需校验通过
  $("watchWaveBtn").disabled = w.st !== "ready";
  const pollBtn = $("watchPollBtn");
  pollBtn.disabled = w.st !== "ready";
  // 文案审查：按钮启停的是观察轮询（列表与会话保留，可再次开始），
  // 不是禁用观察能力，也不是断开会话——用「停止观察」，与「开始观察」对仗
  pollBtn.textContent = w.poll ? "停止观察" : "开始观察";
  pollBtn.classList.toggle("stop", !!w.poll);
  const stats = $("watchStats");
  if (stats) {
    stats.textContent = w.poll
      ? "读 " + w.stats.req + " · 帧 " + w.stats.frm + " · 残帧 " + w.stats.stale
        + " · 超时 " + w.stats.to + " · 预估 " + w.fps + " 帧/秒"
      : "";
    stats.style.color = w.fps > 300 ? "var(--amber)" : "var(--muted)";
  }
  // 实际轮询周期恒显示在期望周期输入框旁（等于期望时也显示，消除随变量
  // 增减的忽隐忽现）；期望为 0 = 停止轮询，无实际周期可言
  const eff = $("watchEffMs");
  if (eff) {
    eff.style.display = w.poll_ms ? "inline" : "none";
    if (w.poll_ms) eff.textContent = "实际 " + w.eff_ms + " ms";
  }
  const set = (id, v) => {
    const el = $(id);
    if (el && document.activeElement !== el && String(el.value) !== String(v)) {
      el.value = v;
    }
  };
  set("watchPollMs", w.poll_ms);
  set("watchGap", w.gap);
  $("watchMerge").checked = !!w.merge;
  $("watchSnap").checked = !!w.snapshot;
}

/* ---------- 符号文件与自动校验 ---------- */
async function watchPickFile() {
  let dir = "";
  try { dir = localStorage.getItem(WATCH_DIR_KEY) || ""; } catch (e) { /* 无存储环境忽略 */ }
  const r = await pywebview.api.watch_pick_file(dir);
  if (!r.success) {
    if (r.message !== "已取消") showToast("error", r.message);
    return;
  }
  if (r.dir) {
    try { localStorage.setItem(WATCH_DIR_KEY, r.dir); } catch (e) { /* 忽略 */ }
  }
  watchFillSymbols(r.symbols);
  // 换文件不清观察列表：后端已按变量名重映射（同名更新地址/类型/宽度，
  // 缺失标记警告色）。表达式行并入同一次整表替换，避免 addData 与
  // replaceData 的异步数据管线交错产生重复行
  if (watch.table && r.table) {
    watch.table.replaceData(r.table.concat(watch.exprs.map(watchExprRowData)));
  }
  if (r.missing && r.missing.length) {
    showToast("warning", "符号缺失 " + r.missing.length + " 个：" + r.missing.join("、"));
  }
  if (state.connected) {
    watchConnect();   // 选择文件即自动校验
  } else {
    showToast("warning", "CAN 未连接：连接 CAN 后重新选择符号文件进行校验");
  }
}

function watchFillSymbols(symbols) {
  watch.symbols = symbols || [];
  const dl = $("watchSymbols");
  dl.innerHTML = "";
  const seen = new Set();
  // datalist = 历史输入（最近的在前）+ 当前符号表，去重合并
  for (const name of watchHistoryNames().concat(watch.symbols)) {
    if (seen.has(name)) continue;
    seen.add(name);
    const o = document.createElement("option");
    o.value = name;
    dl.appendChild(o);
  }
}

function watchHistoryNames() {
  try { return JSON.parse(localStorage.getItem(WATCH_NAME_KEY) || "[]"); }
  catch (e) { return []; }
}

function watchRememberName(name) {
  let names = watchHistoryNames().filter((n) => n !== name);
  names.unshift(name);
  names = names.slice(0, WATCH_NAME_MAX);
  try { localStorage.setItem(WATCH_NAME_KEY, JSON.stringify(names)); } catch (e) { /* 忽略 */ }
  watchFillSymbols(watch.symbols);
}

async function watchConnect() {
  const r = await pywebview.api.watch_connect($("watchAddr").value);
  if (!r.success) showToast("error", r.message);
}

/* ---------- 添加 / 移除 ---------- */
/* 添加一个观察名：后端登记 + 前端建行。silent = 启动恢复用（缺失即跳过，
 * 不弹提示）。表格未初始化时只登记后端，行由 watchRestoreTable 拉整表补齐。 */
async function watchAddName(name, silent) {
  const r = await pywebview.api.watch_add(name);
  if (!r || !r.success) {
    if (!silent) showToast("error", r ? r.message : "添加失败");
    return null;
  }
  if (watch.table) {
    watch.table.addData([r.row], true);
    watchRegisterEnums([r.row]);
  }
  return r;
}

async function watchAdd() {
  const inp = $("watchAddInput");
  const name = (inp.value || "").trim();
  if (!name) return;
  const r = await watchAddName(name, false);
  if (!r) return;
  watchRememberName(name);
  inp.value = "";
  inp.focus();
  watchRememberSession();
}

/* 右键复制：表达式行复制表达式文本；变量行复制观察名（成员行拼全路径，
 * 与写入日志的名称口径一致） */
function watchCopyRow(row) {
  const d = row.getData();
  const id = String(d.id);
  let text = d.name;
  const root = id.match(/^e\d+/);
  const rootRow = root && watch.table.getRow(root[0]);
  if (rootRow && root[0] !== id) {
    text = rootRow.getData().name + id.slice(root[0].length);
  }
  const finish = (ok) => showToast("", ok ? "已复制 " + text : "复制失败");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(() => finish(true), () => finish(false));
    return;
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (err) { ok = false; }
  ta.remove();
  finish(ok);
}

function watchRemoveRow(row) {
  const id = row.getData().id;
  pywebview.api.watch_remove(id);   // 后端先行：删行渲染出错也不会前后端失同步
  if (String(id).startsWith("x")) {
    watch.exprs = watch.exprs.filter((x) => x.id !== id);
    delete watch.stats[id];
    try { row.delete(); } catch (e) { /* 行数据此时已出表，仅渲染抛错，可安全忽略 */ }
    watchRememberSession();
    return;
  }
  const root = (String(id).match(/^e\d+/) || [""])[0];
  for (const k of Object.keys(watch.stats)) {
    if (k === root || k.startsWith(root + ".") || k.startsWith(root + "[")) {
      delete watch.stats[k];
    }
  }
  const r = root && watch.table.getRow(root);
  if (r) {
    try {
      try { r.treeCollapse(); } catch (e) { /* 无子行则忽略 */ }
      r.delete();   // 轮询推送刷新中删展开树形父行会触发 Tabulator 渲染异常，先折叠规避
    } catch (e) { /* 行数据此时已出表，仅渲染抛错，可安全忽略 */ }
  }
  watchRememberSession();
}

async function watchClearAll() {
  await pywebview.api.watch_clear();
  watch.exprs = [];
  watch.stats = {};
  watch.table.clearData();
  watchRememberSession();
}

/* ---------- 表达式行（expr-eval 本地内置） ----------
 * 变量名取当前观察行的最新值代入，随轮询推送刷新；根行按变量名、成员行按
 * 「变量名.成员[下标]」全路径参与代入；引用了不在观察列表里的名字时该行
 * 状态列显示警告。 */
const WATCH_PATH_RE = /[A-Za-z_]\w*(?:\.\w+|\[\d+\])*/g;

/* expr-eval 不认「a.b[2]」形式的路径：把表达式里的变量路径逐个改写成占位名
 * v1、v2…求值时再按占位名代入观察值；back 记录占位名→原路径，供缺变量提示
 * 还原。函数名（后随「(」）与数字字面量尾缀（1e3、0x1F）不改写。 */
function watchRewriteExpr(text) {
  const sub = Object.create(null), back = Object.create(null);
  const out = text.replace(WATCH_PATH_RE, (tok, off, s) => {
    if (/[A-Za-z0-9_$]/.test(s[off - 1] || "")) return tok;
    if (/^\s*\(/.test(s.slice(off + tok.length))) return tok;
    if (!(tok in sub)) {
      const ph = "v" + (Object.keys(sub).length + 1);
      sub[tok] = ph;
      back[ph] = tok;
    }
    return sub[tok];
  });
  return { out: out, back: back };
}

/* 添加表达式文本，返回 null = 成功 / 语法错误信息。表格未初始化时只登记
 * 行数据，行由 watchRestoreTable 拉整表时一并补齐。 */
function watchAddExprText(text) {
  text = (text || "").trim();
  if (!text) return null;
  if (!watch.exprParser) watch.exprParser = new exprEval.Parser();
  const rw = watchRewriteExpr(text);
  let expr;
  try {
    expr = watch.exprParser.parse(rw.out);
  } catch (e) {
    return String(e.message || e);
  }
  const id = "x" + (++watch.exprSeq);
  watch.exprs.push({ id: id, text: text, expr: expr, back: rw.back, last: undefined });
  if (watch.table) {
    watch.table.addData([watchExprRowData(watch.exprs[watch.exprs.length - 1])], false);
  }
  return null;
}

async function watchAddExpr() {
  const inp = $("watchExprInput");
  const text = (inp.value || "").trim();
  if (!text) return;
  const err = watchAddExprText(text);
  if (err) {
    showToast("error", "表达式语法错误：" + err);
    return;
  }
  inp.value = "";
  watchRememberSession();
}

function watchExprRowData(x) {
  return { id: x.id, name: x.text, addr: "", words: "", tname: "表达式",
           val: "—", editable: false, writable: false,
           changed: false, err: "", missing: false };
}

function watchEvalExprs() {
  if (!watch.table || !watch.exprs.length) return;
  const vals = {};
  const rootNames = {};   // 根行 id 前缀 eN -> 变量名（成员行的全路径由它拼出）
  watchWalkRows(watch.table.getRows(), (row) => {
    const d = row.getData();
    const m = /^(e\d+)(.*)$/.exec(String(d.id));
    if (!m) return;
    const isRoot = m[2] === "";
    if (isRoot) rootNames[m[1]] = d.name;
    const path = isRoot ? d.name
      : (rootNames[m[1]] !== undefined ? rootNames[m[1]] + m[2] : null);
    if (path === null || path in vals || d.val === "—" || d.val === undefined) return;
    const v = parseFloat(d.val);
    if (!Number.isNaN(v)) vals[path] = v;
  });
  for (const x of watch.exprs) {
    const row = watch.table.getRow(x.id);
    if (!row) continue;
    let v = null, err = "";
    try {
      const missing = x.expr.variables()
        .filter((n) => vals[x.back[n]] === undefined);
      if (missing.length) {
        err = "未观察变量: " + missing.map((n) => x.back[n]).join("、");
      } else {
        const sub = {};
        for (const ph in x.back) sub[ph] = vals[x.back[ph]];
        v = String(Number(x.expr.evaluate(sub).toPrecision(8)));
      }
    } catch (e) {
      err = String(e.message || e);
    }
    if (v !== null) {
      const changed = x.last !== undefined && x.last !== v;
      watchStatUpdate(x.id, v);
      row.update({ val: v, changed: changed, err: "", minmax: watchStatText(x.id) });
      row.getElement().classList.toggle("watch-chg", changed);
      x.last = v;
    } else {
      row.update({ changed: false, err: err });
      row.getElement().classList.remove("watch-chg");
    }
  }
}

/* ---------- 轮询与选项 ---------- */
async function watchPollToggle() {
  if (watch.st && watch.st.poll) {
    await pywebview.api.watch_stop();
  } else {
    const r = await pywebview.api.watch_start();
    if (!r.success) showToast("error", r.message);
  }
  watchRefreshStatus();
}

async function watchOptionsChanged() {
  const r = await pywebview.api.watch_set_options(
    parseInt($("watchPollMs").value, 10) || 0,
    $("watchMerge").checked,
    parseInt($("watchGap").value, 10) || 0,
    $("watchSnap").checked);
  if (!r.success) showToast("error", r.message);
  else watchRememberSession();
  watchRefreshStatus();
}

/* 推送断流时的兜底：主动拉一次状态 */
async function watchRefreshStatus() {
  try {
    const w = await pywebview.api.watch_status();
    watchOnPush(w);
  } catch (e) { /* 窗口关闭中 */ }
}

/* 添加变量输入框：回车等效点「添加」（手册承诺的交互） */
document.getElementById("watchAddInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") watchAdd();
});
document.getElementById("watchExprInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") watchAddExpr();
});

/* ---------- 观察清单的记住与恢复 ----------
 * 观察清单是独立的名称清单（CCS Watch 同模式）：随时可添加（未载入符号
 * 文件时条目为未解析行，按缺失机制标注），清单不会自动清空。
 * 存档：目标地址 + 变量名列表 + 轮询选项 + 表达式文本，添加/移除/清空/
 * 选项变化/表达式增删后各存一次；固件文件不进存档（加载固件由用户手动
 * 完成，加载并校验通过后清单自动解析或保持缺失标注）。 */
function watchRememberSession() {
  const names = watch.table
    ? watch.table.getData().filter((r) => /^e\d+$/.test(String(r.id))).map((r) => r.name)
    : [];
  const sess = {
    addr: $("watchAddr").value || "01",
    names: names,
    opts: { poll: parseInt($("watchPollMs").value, 10) || 0,
            merge: $("watchMerge").checked,
            gap: parseInt($("watchGap").value, 10) || 0,
            snapshot: $("watchSnap").checked },
    exprs: watch.exprs.map((x) => x.text),
  };
  try { localStorage.setItem(WATCH_SESSION_KEY, JSON.stringify(sess)); }
  catch (e) { /* 无存储环境忽略 */ }
}

/* 启动恢复清单本身：未解析条目由后端登记（行在进入视图时拉整表补齐）。
 * 不自动加载固件文件、不自动连接校验、不自动开始轮询。 */
function watchBootRestore() {
  let sess = null;
  try { sess = JSON.parse(localStorage.getItem(WATCH_SESSION_KEY) || "null"); }
  catch (e) { sess = null; }
  if (!sess) return;
  const has = (sess.names && sess.names.length) || (sess.exprs && sess.exprs.length);
  if (!has) return;
  if (sess.addr) $("watchAddr").value = sess.addr;
  const o = sess.opts || {};
  $("watchPollMs").value = o.poll || 0;
  $("watchMerge").checked = o.merge !== false;
  $("watchGap").value = o.gap || 0;
  $("watchSnap").checked = !!o.snapshot;
  (async () => {
    try {
      await pywebview.api.watch_set_options(
        o.poll || 0, o.merge !== false, o.gap || 0, !!o.snapshot);
    } catch (e) { /* 后端未就绪，选项保持前端值 */ }
    for (const n of sess.names || []) await watchAddName(n, true);
    for (const t of sess.exprs || []) watchAddExprText(t);
  })();
}

if (window.pywebview && window.pywebview.api) watchBootRestore();
else window.addEventListener("pywebviewready", watchBootRestore, { once: true });
