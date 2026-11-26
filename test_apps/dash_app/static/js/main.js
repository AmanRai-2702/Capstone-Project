import { loadStats, addItem } from "./api.js";
import { $ } from "./dom.js";

function showStats(stats) {
  $("#stats").textContent = stats.visits;
}

$("#refresh").addEventListener("click", async () => {
  const stats = await loadStats();
  showStats(stats);
  const charts = await import("./charts.js");
  charts.drawChart(stats);
});

document.getElementById("add").addEventListener("click", () => {
  addItem({ name: "x" });
});

function hover() {}

$("#stats").onclick = () => showStats({ visits: 0 });
document.getElementById("chart").onmouseover = hover;
