/* 主题插件：明亮（light）—— 暖纸白底 + 墨绿强调
 * 保持仪器面板在明亮环境下的读数对比度。
 * 页面样式见同目录 light.css（.theme-light）；本文件提供元数据与 canvas 图表配色。 */
window.ThemeRegistry.register({
  id: "light",
  label: "明亮",
  chart: {
    v: "#0B7A52",        // 电压轨迹：深翠绿（白底上达 WCAG AA）
    i: "#0E7490",        // 电流轨迹：深青
    tick: "#6B7870",
    grid: "#E2E5DD",
    border: "#C8CDC2",
    font: "Cascadia Mono, Consolas, monospace",
  },
});
