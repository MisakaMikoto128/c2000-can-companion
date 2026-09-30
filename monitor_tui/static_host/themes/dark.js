/* 主题插件：暗黑（dark）—— 数字仪器面板
 * 深色底 / 磷光绿读数 / 等宽数字 / 发丝线分隔。
 * 页面样式见同目录 dark.css（.theme-dark）；本文件提供元数据与 canvas 图表配色。 */
window.ThemeRegistry.register({
  id: "dark",
  label: "暗黑",
  chart: {
    v: "#35F0A0",        // 电压轨迹：磷光绿
    i: "#3EC6E0",        // 电流轨迹：青
    tick: "#7C8A99",     // 刻度文字
    grid: "#1A212B",     // 网格线
    border: "#232B36",   // 坐标轴线
    font: "Cascadia Mono, Consolas, monospace",
  },
});
