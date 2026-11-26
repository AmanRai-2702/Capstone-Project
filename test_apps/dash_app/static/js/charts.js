import { $ } from "./dom.js";

export function drawChart(data) {
  const canvas = $("#chart");
  canvas.title = String(data.visits);
}
