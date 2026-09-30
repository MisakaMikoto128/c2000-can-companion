/* CAN Companion 前端 — 升级 / WATCH / 设置 / 关于
 * 后端推送 onPush({s,p,w})：s=连接状态 p=升级进度 w=WATCH 快照；
 * 1s 低频轮询兜底，推送断流时界面不至于死掉。 */

const state = {
  connected: false,
  modules: [],
  addr: null,
  upgradeAddr: (function () {
    try { return localStorage.getItem("ccUpgradeAddr") || "0x01"; }
    catch (e) { return "0x01"; }
  })(),
  fwLoaded: false,
  blFwLoaded: false,   // Bootloader 固件已选（不跨启动恢复，每次现选现烧）
  running: false,
  logCount: 0,
  blocksTotal: 0,
  confirmSeq: 0,
  batchRunning: false,   // 批量升级进行中（结束转汇总 toast，不走单机结果面板）
  view: "upgrade",
};

const $ = id => document.getElementById(id);

/* ---------- 视图切换 ---------- */
async function switchView(name) {
  state.view = name;
  document.querySelectorAll(".view").forEach(v => v.classList.remove("active"));
  $("view-" + name).classList.add("active");
  document.querySelectorAll(".rail-btn").forEach(b =>
    b.classList.toggle("active", b.dataset.view === name));
  // 升级页要显示设备在哪一侧，切进来就重扫一轮总线（升级进行中不插队）
  if (name === "upgrade" && state.connected && !state.running) reprobe();
  // 观察页：表格在页面可见时才建（隐藏态建表拿不到正确布局）
  if (name === "watch") { watchInitTable(); watchRefreshStatus(); }
}

/* ---------- Toast ---------- */
function showToast(kind, msg) {
  const t = document.createElement("div");
  t.className = "toast" + (kind === "error" ? " error" : kind === "warning" ? " warning" : "");
  t.textContent = msg;
  $("toastContainer").appendChild(t);
  setTimeout(() => t.remove(), 3000);
}

/* ---------- 连接 ---------- */
async function reprobe() {
  const r = await pywebview.api.reprobe();
  if (!r.success) showToast("error", r.message);
}

function syncModules(modules, appAddr) {
  const next = modules || [];
  if (JSON.stringify(next) === JSON.stringify(state.modules)
      && appAddr === state.appAddr) return;
  state.modules = next;
  state.appAddr = appAddr;
  if (state.addr && !state.modules.includes(state.addr)) state.addr = null;
  syncAddrOptions();
  renderConn();
}

/* 地址候选下拉（upgradeAddrInput / watchAddr 共用的 datalist）：
 * 探测到的设备 + 在跑 App 的地址，短格式"01" */
function syncAddrOptions() {
  const items = new Set();
  for (const m of state.modules) items.add(m.replace("0x", ""));
  if (state.appAddr) items.add(state.appAddr.replace("0x", ""));
  const dl = $("scanAddrOptions");
  const sig = [...items].sort().join(",");
  if (dl.dataset.sig === sig) return;
  dl.dataset.sig = sig;
  dl.innerHTML = "";
  for (const v of [...items].sort()) {
    const o = document.createElement("option");
    o.value = v;
    dl.appendChild(o);
  }
}

function syncBlInfo(blInfo) {
  const el = $("blInfo");
  if (state.addr && blInfo) {
    el.style.display = "";
    el.textContent =
      `Bootloader ${blInfo.version} · 扇区 ${blInfo.sector_nb} · 参数区 SEC${blInfo.param_sector}`;
  } else {
    el.style.display = "none";
  }
}

function renderConn() {
  $("modEmpty").style.display = state.modules.length ? "none" : "";
  const list = $("modList");
  list.innerHTML = "";
  for (const m of state.modules) {
    const c = document.createElement("span");
    c.className = "mchip" + (m === state.addr ? " sel" : "");
    c.textContent = m;
    c.onclick = () => selectModule(m);
    list.appendChild(c);
  }
  if (state.modules.length && !state.addr) selectModule(state.modules[0]);
  refreshButtons();
}

async function selectModule(m) {
  state.addr = m;
  state.upgradeAddr = m;
  const ua = $("upgradeAddrInput");
  if (ua) ua.value = m.replace("0x", "");
  try { localStorage.setItem("ccUpgradeAddr", m); } catch (e) { /* 忽略 */ }
  document.querySelectorAll("#modList .mchip").forEach(c =>
    c.classList.toggle("sel", c.textContent === m));
  const r = await pywebview.api.get_info(parseInt(m, 16));
  if (!r.success) showToast("error", r.message);
  refreshButtons();
}

/* ---------- 状态条 ---------- */
function renderStatusBar(s) {
  $("sbConnDot").className = "dot" + (s.connected ? " on" : "");
  $("sbConnText").textContent = s.connected ? "已连接" : "未连接";
  $("sbStats").textContent = `RX ${s.rx_total} · TX ${s.tx_ok}` +
    (s.tx_fail ? ` · 失败 ${s.tx_fail}` : "");
  let dev = "—";
  if (s.bl_info) dev = "BL " + s.bl_info.version;
  else if (s.dev_state === "bl") dev = "Bootloader";
  else if (s.dev_state === "app") dev = "App";
  $("sbDev").textContent = dev;
  $("sbVer").textContent = s.app_info ? s.app_info.version : "—";
  $("sbBuild").textContent = s.app_build_time || "—";
  // 连接配置动态化（设置页可改波特率/通道；后端状态随推送带回）
  if (typeof s.baud === "number") {
    $("sbPort").textContent = `USBCAN2 ch${s.channel} · ${s.baud / 1000}k`;
  }
}

/* ---------- 设备侧状态（升级页）----------
 * App 损坏时 Bootloader 校验不过就不跳转、会一直停在那里等烧写，这是恢复的唯一入口，
 * 所以停在 Bootloader 要显眼地讲清楚，不能只显示一个"BL"。 */
function renderDevState(s) {
  const el = $("devState"), hint = $("devStateHint");
  let text, tip = "", warn = false;
  if (!s.connected) {
    text = "未连接";
    tip = "连接 CAN 之后会自动探测设备在哪一侧。";
  } else if (s.dev_state === "bl") {
    text = "等待固件写入" + (s.modules.length ? `（地址 ${s.modules.join("、")}）` : "");
    warn = true;
    tip = "设备在升级模式等待写入。如果不是主动升级，说明设备程序没能正常启动，"
        + "点「开始升级」写入固件即可恢复。";
  } else if (s.dev_state === "app" && s.app_info) {
    text = `设备运行中 · 版本 ${s.app_info.version}`
        + (s.app_build_time ? `（编译 ${s.app_build_time}）` : "");
    tip = "点「开始升级」更新固件：设备会复位进入 Bootloader，升级完成后自动回到应用。";
  } else if (s.dev_state === "timeout") {
    text = "没有发现设备";
    tip = "请检查 CAN 接线、波特率和设备供电，确认设备已上电。";
  } else {
    text = "正在探测…";
  }
  el.textContent = text;
  el.className = "dev-state" + (warn ? " warn" : "");
  hint.textContent = tip;
}

/* ---------- 关于视图 ---------- */
function renderAbout(s) {
  if (!s) return;
  const t = (el, v) => { const e = $(el); if (e && e.textContent !== (v || "—")) e.textContent = v || "—"; };
  t("#aboutVersion", s.app_version);
  t("#aboutBuild", s.build_time);
  t("#aboutFwVer", s.app_info ? ("V" + s.app_info.version) : "—");
}

/* ---------- 升级视图 ---------- */
const STAGE_ORDER = ["erase", "write", "verify", "run"];
const PROBE_STAGES = ["probe", "confirm"];   // 还没碰到设备，都算"连接设备"这一步

function refreshButtons() {
  const gate = state.connected && state.fwLoaded && !state.running;
  $("startBtn").disabled = !gate;
  $("startBtn").title = !state.connected ? "需要先连接 CAN"
    : !state.fwLoaded ? "先选择固件文件" : "";
  const autoOn = !!($("autoFlashSw") && $("autoFlashSw").checked);
  $("batchBtn").disabled = !gate || autoOn;
  $("batchBtn").title = autoOn ? "自动烧录仅针对单机，多机批量不支持自动触发"
    : !state.connected ? "需要先连接 CAN"
    : !state.fwLoaded ? "先选择固件文件" : "";
  const blBtn = $("blStartBtn");
  if (blBtn) {
    blBtn.disabled = !(state.connected && state.blFwLoaded && !state.running);
    blBtn.title = !state.connected ? "需要先连接 CAN"
      : !state.blFwLoaded ? "先选择 Bootloader 固件" : "";
  }
}

async function pickFile() {
  const r = await pywebview.api.pick_file();
  if (!r.success) { if (r.message !== "已取消") showToast("error", r.message); return; }
  // 记住完整路径：下次启动静默恢复（restore_firmware），升级前也按它强制重载
  try { localStorage.setItem("ccFwPath", r.file.path); } catch (e) { /* 忽略 */ }
  state.fwLoaded = true;
  showFileChip(r);
  refreshButtons();
}

/* 固件文件 chip（pickFile 与启动恢复共用同一渲染） */
function showFileChip(r) {
  if (!r || !r.file) return;
  $("fileChip").style.display = "";
  $("fwName").textContent = r.file.name;
  const mtime = r.file.mtime ? ` · 修改于 ${r.file.mtime}` : "";
  $("fwMeta").textContent = `${r.file.format} · ${r.file.size.toLocaleString()} octet · 基址 ${r.file.base_addr}${mtime}`;
  $("fwCrc").textContent = "CRC32 " + r.file.crc32;
}

/* 开始升级：没选定设备地址也照常启动，由探测决定要跟谁说话 */
async function startUpgrade() {
  const raw = ($("upgradeAddrInput").value || "").trim();
  const v = parseInt(raw, 16);
  const target = (!isNaN(v) && v >= 0x01 && v <= 0x3B)
    ? "0x" + v.toString(16).toUpperCase().padStart(2, "0") : "";
  if (target && target !== state.upgradeAddr) {
    state.upgradeAddr = target;
    try { localStorage.setItem("ccUpgradeAddr", target); } catch (e) { /* 忽略 */ }
  }
  const r = await pywebview.api.start_upgrade(target || "",
                                              parseInt($("probeTimeout").value, 10),
                                              $("compressSw").checked);
  if (!r.success) { showToast("error", r.message); return; }
  state.running = true;
  $("cancelBtn").style.display = "";
  $("resultPanel").style.display = "none";
  refreshButtons();
}

async function cancelUpgrade() { await pywebview.api.cancel(); }

/* ---------- Bootloader 升级（RAM 烧录代理由后端自带，用户只选 BL 固件） ---------- */
async function pickBlFile() {
  const r = await pywebview.api.pick_bl_file();
  if (!r.success) { if (r.message !== "已取消") showToast("error", r.message); return; }
  state.blFwLoaded = true;
  if (r.file) {
    $("blFileChip").style.display = "";
    $("blFwName").textContent = r.file.name;
    const mtime = r.file.mtime ? ` · 修改于 ${r.file.mtime}` : "";
    $("blFwMeta").textContent = `${r.file.format} · ${r.file.size.toLocaleString()} octet${mtime}`;
    $("blFwCrc").textContent = "CRC32 " + r.file.crc32;
  }
  refreshButtons();
}

function blStart() {
  if (!state.connected) { showToast("error", "请先连接 CAN"); return; }
  if (!state.blFwLoaded) { showToast("error", "请先选择 Bootloader 固件"); return; }
  if (state.running) { showToast("error", "升级进行中"); return; }
  $("blModal").style.display = "flex";
}

async function blConfirm(ok) {
  $("blModal").style.display = "none";
  if (!ok) return;
  const raw = ($("upgradeAddrInput").value || "").trim();
  const v = parseInt(raw, 16);
  const target = (!isNaN(v) && v >= 0x01 && v <= 0x3B)
    ? "0x" + v.toString(16).toUpperCase().padStart(2, "0") : "";
  const r = await pywebview.api.upgrade_bootloader(target || "",
                                                   parseInt($("probeTimeout").value, 10));
  if (!r.success) { showToast("error", r.message); return; }
  state.running = true;
  $("cancelBtn").style.display = "";
  $("resultPanel").style.display = "none";
  refreshButtons();
}

/* ---------- 批量升级（升级全部设备，逐台执行） ---------- */
let batchConfirmStage = 0;   // 两遍确认：1=第一遍，2=第二遍，0=未在确认

function batchStart() {
  if (!state.connected) { showToast("error", "请先连接 CAN"); return; }
  if (!state.fwLoaded) { showToast("error", "请先选择固件文件"); return; }
  if (state.running) { showToast("error", "升级进行中"); return; }
  batchConfirmStage = 1;
  $("batchModalText").textContent =
    "批量升级会让总线上所有设备复位进入 Bootloader 并逐一升级固件，确定继续？";
  $("batchModal").style.display = "flex";
}

async function batchConfirm(ok) {
  $("batchModal").style.display = "none";
  if (!ok) { batchConfirmStage = 0; return; }
  if (batchConfirmStage === 1) {
    batchConfirmStage = 2;
    $("batchModalText").textContent = "再次确认：所有设备将复位进入 Bootloader";
    $("batchModal").style.display = "flex";
    return;
  }
  batchConfirmStage = 0;
  const r = await pywebview.api.upgrade_all(parseInt($("probeTimeout").value, 10),
                                            $("compressSw").checked);
  if (!r.success) { showToast("error", r.message); return; }
  state.running = true;
  state.batchRunning = true;
  $("cancelBtn").style.display = "";
  $("resultPanel").style.display = "none";
  refreshButtons();
}

/* 批量升级表：地址/状态/进度%，正在烧写的一行高亮（完整 message 放 title 悬停可见） */
function renderBatch(p) {
  const ua = p.upgrade_all;
  const panel = $("batchPanel");
  if (!ua || !ua.devices || !ua.devices.length) {
    if (!state.batchRunning) panel.style.display = "none";
    return;
  }
  panel.style.display = "";
  const body = $("batchBody");
  body.innerHTML = "";
  let done = 0;
  for (const d of ua.devices) {
    if (d.state === "完成") done++;
    const tr = document.createElement("tr");
    if (ua.current === d.addr) tr.className = "cur";
    const td = v => { const c = document.createElement("td"); c.textContent = v; return c; };
    const st = td(d.state);
    if (d.message) st.title = d.addr + " " + d.state + "：" + d.message;
    tr.appendChild(td(d.addr));
    tr.appendChild(st);
    tr.appendChild(td((d.percent || 0) + "%"));
    body.appendChild(tr);
  }
  $("batchSummary").textContent = done + "/" + ua.devices.length + " 台完成";
}

/* 确认弹窗：把后台线程挂起的升级流程答复给它，界面立刻收掉弹窗避免重复点 */
async function answerConfirm(accept) {
  const r = await pywebview.api.confirm_upgrade(accept);
  if (!r.success) { showToast("error", r.message); return; }
  $("confirmModal").style.display = "none";
  showToast(accept ? "warning" : "",
            accept ? "已请求设备进入升级模式，等待 Bootloader 应答"
                   : "已取消：设备继续运行当前固件");
}

function renderConfirm(c) {
  const m = $("confirmModal");
  if (!c) { m.style.display = "none"; return; }
  if (state.confirmSeq !== c.seq) {
    state.confirmSeq = c.seq;
    $("confirmText").textContent = c.text;
  }
  m.style.display = "flex";
}

function renderUpgrade(p) {
  renderConfirm(p.confirm);
  renderBatch(p);

  // 步进条
  const stageIdx = STAGE_ORDER.indexOf(p.stage);
  const probing = PROBE_STAGES.includes(p.stage);
  const set = (s, cls) => { document.querySelector(`.sstep[data-s="${s}"]`).className = "sstep" + (cls ? " " + cls : ""); };
  set("file", state.fwLoaded ? "done" : "active");
  set("conn", probing ? "active" : (state.addr ? "done" : (state.connected ? "active" : "")));
  if (p.success === true) {
    set("write", "done"); set("verify", "done"); set("run", "done");
  } else if (p.success === false && probing) {
    set("conn", "fail");
    set("write", ""); set("verify", ""); set("run", "");
  } else if (p.success === false && stageIdx >= 0) {
    set("write", stageIdx > 1 ? "done" : (stageIdx <= 1 ? "fail" : ""));
    set("verify", stageIdx === 2 ? "fail" : (stageIdx > 2 ? "done" : ""));
    set("run", stageIdx === 3 ? "fail" : "");
    if (stageIdx <= 1) { set("verify", ""); set("run", ""); }
    if (stageIdx === 0 || stageIdx === 1) set("write", "fail");
  } else if (p.running) {
    set("write", stageIdx <= 1 ? "active" : "done");
    set("verify", stageIdx === 2 ? "active" : (stageIdx > 2 ? "done" : ""));
    set("run", stageIdx === 3 ? "active" : "");
  } else {
    set("write", ""); set("verify", ""); set("run", "");
  }

  // 块进度格：本轮没有块（还没进入写入，或已复位）就把上一轮的格子清掉
  if (p.blocks_total === 0 && state.blocksTotal !== 0) {
    state.blocksTotal = 0;
    $("blocks").innerHTML = "";
  }
  if (p.blocks_total > 0) {
    if (p.blocks_total !== state.blocksTotal) {
      state.blocksTotal = p.blocks_total;
      const box = $("blocks");
      box.innerHTML = "";
      for (let i = 0; i < p.blocks_total; i++) {
        const d = document.createElement("div");
        d.className = "blk";
        box.appendChild(d);
      }
    }
    document.querySelectorAll("#blocks .blk").forEach((el, i) => {
      el.className = "blk" + (i < p.blocks_done ? " done"
        : (i === p.blocks_done && p.running ? " cur" : ""))
        + (p.blocks_retry === i ? " retry" : "");
    });
  }
  $("pPct").textContent = p.percent ? p.percent + "%" : "";
  $("stageMsg").textContent = p.message || "选择固件并连接设备后开始";
  $("stageMsg").style.color = (p.success === false && !p.cancelled) ? "var(--red)" : "";
  // 升级方式常驻行：增量升级（跳过 N/M 块）/ 全量升级——探测规划后即显示
  const um = $("upgradeMode");
  if (p.upgrade_mode) { um.textContent = "升级方式：" + p.upgrade_mode; um.style.display = ""; }
  else { um.style.display = "none"; }

  if (state.running && !p.running) {
    state.running = false;
    $("cancelBtn").style.display = "none";
    if (state.batchRunning) {
      // 批量升级结束：汇总走 toast，结果明细常驻批量面板
      state.batchRunning = false;
      const ua = p.upgrade_all;
      if (ua && ua.devices && ua.devices.length) {
        if (p.cancelled) showToast("warning", p.message || "批量升级已取消");
        else if (p.success === true) showToast("success", p.message || "批量升级完成");
        else showToast("warning", p.message || "批量升级结束，有设备未完成");
      } else {
        showToast("error", p.message || "批量升级失败");   // 发现阶段就没找到设备
      }
    } else if (p.success === true) {
      $("resultPanel").style.display = "";
      $("resultText").textContent = "✓ " + p.message;
      showToast("success", "升级完成");
    } else if (p.cancelled) {
      showToast("warning", p.message);
    } else {
      showToast("error", p.message);
    }
    refreshButtons();
  }
}

/* ---------- 运行日志 ---------- */
function renderLogs(logs) {
  if (logs.length < state.logCount) { state.logCount = 0; $("logTerm").innerHTML = ""; }
  const term = $("logTerm");
  for (let i = state.logCount; i < logs.length; i++) {
    const d = document.createElement("div");
    d.textContent = logs[i];
    term.appendChild(d);
  }
  state.logCount = logs.length;
  term.scrollTop = term.scrollHeight;
}

function clearLog() {
  $("logTerm").innerHTML = "";
  state.logCount = 0;
}

/* ---------- 后端推送与兜底轮询 ---------- */
/* 后端推送：状态快照变化 ≤50ms 推到这里（主通道，替代状态轮询） */
window.onPush = (d) => {
  try {
    const s = d.s, p = d.p;
    syncModules(s.modules, s.app_info ? s.app_info.addr : null);
    syncBlInfo(s.bl_info);
    renderStatusBar(s);
    renderDevState(s);
    renderAbout(s);
    renderUpgrade(p);
    renderLogs(p.logs || []);
    if (d.w) watchOnPush(d.w);
  } catch (e) { /* 渲染异常不炸推送通道 */ }
};

async function poll() {
  /* 低频兜底（1s）：全量重渲染一遍——与推送幂等，推送通道断流时界面不至于死掉 */
  try {
    const [s, p] = await Promise.all([
      pywebview.api.get_status(), pywebview.api.get_progress()]);
    syncModules(s.modules, s.app_info ? s.app_info.addr : null);
    syncBlInfo(s.bl_info);
    renderStatusBar(s);
    renderDevState(s);
    renderAbout(s);
    renderUpgrade(p);
    renderLogs(p.logs || []);
  } catch (e) { /* 窗口关闭中 */ }
  setTimeout(poll, 1000);
}

/* ---------- 启动 ---------- */
function whenApiReady() {
  return new Promise(res => {
    if (window.pywebview && window.pywebview.api) return res();
    window.addEventListener("pywebviewready", () => res(), { once: true });
  });
}

/* 用户习惯持久化：这些控件的值关软件重开仍保留。
   checkbox 存的是勾选态（el.checked → "1"/"0"），其余控件存 value。 */
const PERSIST_INPUTS = ["probeTimeout", "upgradeAddrInput", "compressSw", "autoFlashSw"];

function restorePersisted() {
  for (const id of PERSIST_INPUTS) {
    const el = $(id);
    if (!el) continue;
    const isCheck = el.type === "checkbox";
    let v = null;
    try { v = localStorage.getItem("cc_" + id); } catch (e) { /* 忽略 */ }
    if (isCheck) {
      if (v !== null) el.checked = v === "1";
    } else if (v !== null && v !== "") el.value = v;
    el.addEventListener("change", () => {
      try { localStorage.setItem("cc_" + id, isCheck ? (el.checked ? "1" : "0")
                                                      : el.value); }
      catch (e) { /* 忽略 */ }
    });
  }
  const ua = $("upgradeAddrInput");
  if (ua && state.upgradeAddr) ua.value = state.upgradeAddr.replace("0x", "");
}

/* ---------- 自动烧录（仅单机） ---------- */
function updateAfHint() {
  $("afHint").style.display = $("autoFlashSw").checked ? "" : "none";
}
async function autoFlashChanged(silent) {
  const on = $("autoFlashSw").checked;
  const r = await pywebview.api.set_auto_flash(on, ($("upgradeAddrInput").value || "").trim(),
                                              $("compressSw").checked, silent === true);
  updateAfHint();
  if (!r.success) {
    showToast("error", r.message);
    $("autoFlashSw").checked = !on;          // 后端拒绝就回弹开关
    return;
  }
  if (silent !== true)                       // 启动恢复静默，不弹提示
    showToast(on ? "success" : "", on ? "已开启自动烧录：固件文件更新后自动写入（仅单台）"
                                      : "已关闭自动烧录");
  refreshButtons();                          // 开启时禁用「升级全部设备」
}

/* ---------- 设置页：外观主题（薄包装，逻辑在 ThemeRegistry） ---------- */
function applyTheme(t) {
  return window.ThemeRegistry ? ThemeRegistry.apply(t) : null;
}
async function themeChanged() {
  const val = $("themeSel").value;
  if (window.ThemeRegistry) {
    const t = ThemeRegistry.select(val);
    showToast("", "已切换到「" + t.label + "」主题");
    return;
  }
  /* 降级路径：注册表脚本未加载时，直接切换 html 主题类 + 持久化 + 重涂波形 */
  try { localStorage.setItem("ccTheme", val); } catch (e) { /* 忽略 */ }
  const root = document.documentElement;
  ["dark", "light"].forEach(k => root.classList.remove("theme-" + k));
  root.classList.add("theme-" + val);
  if (typeof watchWaveRecolor === "function") watchWaveRecolor();
  const labels = { dark: "暗黑", light: "明亮" };
  showToast("", "已切换到「" + (labels[val] || val) + "」主题");
}

/* ---------- 设置页：CAN 连接配置 ---------- */
function restoreLinkConfig() {
  let b = null, c = null;
  try { b = localStorage.getItem("ccBaud"); c = localStorage.getItem("ccChannel"); }
  catch (e) { /* 忽略 */ }
  if (b && $("baudSel").querySelector(`option[value="${b}"]`)) $("baudSel").value = b;
  if (c && $("channelSel").querySelector(`option[value="${c}"]`)) $("channelSel").value = c;
}

async function linkConfigChanged() {
  const baud = $("baudSel").value, channel = $("channelSel").value;
  try {
    localStorage.setItem("ccBaud", baud);
    localStorage.setItem("ccChannel", channel);
  } catch (e) { /* 忽略 */ }
  const r = await pywebview.api.set_link_config(parseInt(baud, 10), parseInt(channel, 10));
  if (!r.success) {
    showToast("error", r.message);
    // 回显后端实际生效的配置，避免下拉框与设备状态脱节
    const s = await pywebview.api.get_status();
    $("baudSel").value = String(s.baud);
    $("channelSel").value = String(s.channel);
    return;
  }
  showToast(r.reconnected ? "success" : "", r.reconnected
    ? "已断开并按新配置重连（通道 " + channel + " · " + (parseInt(baud, 10) / 1000) + "k）"
    : "已保存，下次连接 CAN 时生效");
}

window.addEventListener("DOMContentLoaded", async () => {
  // 主题先于首帧应用：注册表构建下拉框并应用记忆主题（真正的首帧防闪烁由
  // <head> 内联脚本在 html 上加 .theme-<id> 类完成）
  try { ThemeRegistry.init(); } catch (e) { /* 注册表异常不阻断启动 */ }
  if (window.ThemeRegistry) {
    ThemeRegistry.onChange(() => {
      if (typeof watchWaveRecolor === "function") watchWaveRecolor();
    });
  }
  restorePersisted();
  // 注意：这里还在 DOMContentLoaded，pywebview 尚未注入 api，任何 pywebview.api
  // 调用都会同步抛 TypeError 让整个初始化（含自动连接）静默死掉——api 调用一律放
  // 在下方 await whenApiReady() 之后。
  await whenApiReady();
  // 恢复上次选择的固件：文件还在就静默重载（chip 照常显示），不在就清掉记录
  try {
    const fwPath = localStorage.getItem("ccFwPath");
    if (fwPath) {
      const rr = await pywebview.api.restore_firmware(fwPath);
      if (rr && rr.success) { state.fwLoaded = true; showFileChip(rr); }
      else if (rr && rr.missing) localStorage.removeItem("ccFwPath");
    }
  } catch (e) { /* 忽略 */ }
  restoreLinkConfig();
  poll();
  // 自动连接：带上设置页记忆的波特率/通道（没存过就传 null，后端用默认存值）
  let baud0 = null, chan0 = null;
  try {
    baud0 = parseInt(localStorage.getItem("ccBaud") || "", 10) || null;
    const c = parseInt(localStorage.getItem("ccChannel") || "", 10);
    if (c === 0 || c === 1) chan0 = c;
  } catch (e) { /* 忽略 */ }
  let r = await pywebview.api.connect(baud0, chan0);
  // 连接失败自动重试：启动瞬间旧实例的 CAN 句柄可能尚未释放（赛跑场景）
  for (let i = 0; i < 9 && !r.success; i++) {
    showToast("warning", "CAN 连接失败，2 秒后自动重试（" + (i + 1) + "/9）："
              + (r.message || "未知原因"));
    await new Promise(res => setTimeout(res, 2000));
    r = await pywebview.api.connect(baud0, chan0);
  }
  if (r.success) {
    state.connected = true;
    renderConn();
    // 自动烧录开关随持久化恢复：上次退出时开着就重新武装看门狗（静默，不弹提示）
    updateAfHint();
    if ($("autoFlashSw").checked) autoFlashChanged(true).catch(() => {});
  } else {
    showToast("error", r.message);
  }
});
