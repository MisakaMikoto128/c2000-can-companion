/* 主题注册表（ThemeRegistry）—— 主题插件化的唯一入口。
 *
 * 一个主题 = 一个自包含插件包，放在本目录下：
 *   themes/<id>.js    元数据：id / label / chart（canvas 图表配色），调用 register() 注册
 *   themes/<id>.css   页面样式：以 .theme-<id> 为作用域，通常只覆盖 CSS 变量，
 *                     需要深度改版（如海报主题）时可写任意结构覆盖
 *
 * 新增主题三步：① 放 <id>.js / <id>.css 两个文件；
 *               ② index.html 的 <head> 加一行 <link>、底部加一行 <script>；
 *               ③ 无需改动任何业务代码，下拉框自动出现新主题。
 *
 * 主题类挂在 <html>（documentElement）上：head 内联脚本在首帧之前就加上类，
 * CSS 变量从 html 继承到全文档，切换主题无闪烁、无网络请求（file:// 下同样可靠）。 */
window.ThemeRegistry = (function () {
  const STORE_KEY = "ccTheme";
  const FALLBACK = "dark";
  const themes = new Map();   // id -> 主题对象（按注册顺序保留）
  let currentId = null;
  const listeners = [];       // 主题切换回调，如 canvas 图表重涂

  return {
    /* 注册主题插件。最小字段 { id }；label 默认取 id，chart 可为 null */
    register(theme) {
      if (!theme || !theme.id) throw new Error("ThemeRegistry: 主题缺少 id");
      const t = Object.assign({ label: theme.id, chart: null }, theme);
      themes.set(t.id, t);
      return t;
    },
    list() { return [...themes.values()]; },
    get(id) { return themes.get(id); },
    current() { return themes.get(currentId) || null; },
    /* 当前主题的 canvas 图表配色（供 Chart.js 取色） */
    chartColors() {
      const t = themes.get(currentId);
      return t ? t.chart : null;
    },
    /* 注册切换回调，apply 成功后以主题对象为参数调用 */
    onChange(fn) { if (typeof fn === "function") listeners.push(fn); },

    /* 应用主题：切换 html 上的 .theme-<id> 类、同步下拉框、通知监听者。
     * id 不存在时回落到 FALLBACK，再不行取第一个已注册主题 */
    apply(id) {
      let t = themes.get(id) || themes.get(FALLBACK);
      if (!t) t = [...themes.values()][0];
      if (!t) return null;   /* 无任何主题插件时不抛错，由调用方降级 */
      const root = document.documentElement;
      [...themes.keys()].forEach(k => root.classList.remove("theme-" + k));
      root.classList.add("theme-" + t.id);
      currentId = t.id;
      const sel = document.getElementById("themeSel");
      if (sel) sel.value = t.id;
      listeners.forEach(fn => { try { fn(t); } catch (e) { /* 监听者异常不影响切换 */ } });
      return t;
    },

    /* 用户在下拉框中选定：应用 + 持久化，返回主题对象 */
    select(id) {
      const t = this.apply(id);
      try { localStorage.setItem(STORE_KEY, t.id); } catch (e) { /* 无存储环境忽略 */ }
      return t;
    },

    /* 启动初始化：合并填充下拉框，再应用记忆中的主题。
     * 下拉框在 HTML 中带有内置主题的静态 option（兜底），这里只追加
     * “已注册但 select 中尚不存在”的插件主题、不做清空 —— 即使本注册表
     * 完全没加载，用户仍可用内置主题；由 app.js 在图表建表之前调用。 */
    init() {
      const sel = document.getElementById("themeSel");
      if (sel && sel.dataset.merged !== "1") {
        const have = new Set([...sel.options].map(o => o.value));
        themes.forEach(t => {
          if (have.has(t.id)) return;
          const o = document.createElement("option");
          o.value = t.id;
          o.textContent = t.label;
          sel.appendChild(o);
        });
        sel.dataset.merged = "1";
      }
      let saved = FALLBACK;
      try { saved = localStorage.getItem(STORE_KEY) || FALLBACK; } catch (e) { /* 忽略 */ }
      return this.apply(saved);
    },
  };
})();
