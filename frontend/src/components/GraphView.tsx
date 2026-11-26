import cytoscape from "cytoscape";
import { useEffect, useRef } from "react";
import type { GraphData } from "../api/types";

export const TYPE_COLORS: Record<string, string> = {
  file: "#8a94a6", function: "#3b5bdb", method: "#5f3dc4", class: "#d9480f", api_endpoint: "#0b7285",
  html_element: "#2b8a3e", css_rule: "#c2255c", variable: "#868e96", module: "#495057",
};
const colorOf = (t: string) => TYPE_COLORS[t] ?? "#868e96";

/** Cytoscape wrapper. Node/edge clicks are reported to the parent; the graph is rebuilt when `data` changes. */
export function GraphView({ data, selected, highlight, onNode, onEdge, layout = "cose" }: {
  data: GraphData; selected?: string | null; highlight?: Set<string>;
  onNode?: (id: string) => void; onEdge?: (e: { source: string; type: string; target: string }) => void; layout?: "cose" | "breadthfirst" | "circle";
}) {
  const host = useRef<HTMLDivElement>(null);
  const cy = useRef<cytoscape.Core | null>(null);
  const cbNode = useRef(onNode); cbNode.current = onNode;
  const cbEdge = useRef(onEdge); cbEdge.current = onEdge;

  useEffect(() => {
    if (!host.current) return;
    const instance = cytoscape({
      container: host.current,
      elements: [
        ...data.nodes.map((n) => ({ data: { id: n.id, label: n.label.split("/").pop() ?? n.label, type: n.type, color: colorOf(n.type), degree: n.degree } })),
        ...data.edges.map((e, i) => ({ data: { id: e.id ?? `e${i}`, source: e.source, target: e.target, type: e.type, kind: e.kind, conf: e.confidence } })),
      ],
      style: [
        { selector: "node", style: { "background-color": "data(color)", label: "data(label)", "font-size": 9, color: "#8a94a6", "text-valign": "bottom", "text-margin-y": 3,
            width: "mapData(degree, 0, 30, 14, 38)", height: "mapData(degree, 0, 30, 14, 38)", "text-max-width": "90px", "text-wrap": "ellipsis" } },
        { selector: "edge", style: { width: 1.2, "line-color": "#a5adba", "target-arrow-color": "#a5adba", "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": 0.8, opacity: 0.8 } },
        { selector: "edge[kind = 'inferred']", style: { "line-style": "dashed" } },
        { selector: "edge[kind = 'heuristic']", style: { "line-style": "dotted", "line-color": "#e5a03b", "target-arrow-color": "#e5a03b" } },
        { selector: ".sel", style: { "border-width": 3, "border-color": "#f59f00" } },
        { selector: ".hl", style: { "border-width": 3, "border-color": "#e03131", "line-color": "#e03131", "target-arrow-color": "#e03131" } },
      ],
      layout: data.nodes.length > 400 ? { name: "grid" } : layout === "cose" ? { name: "cose", animate: false, nodeRepulsion: () => 9000 } : { name: layout },
      wheelSensitivity: 0.25,
    });
    instance.on("tap", "node", (ev) => cbNode.current?.(ev.target.id()));
    instance.on("tap", "edge", (ev) => cbEdge.current?.({ source: ev.target.data("source"), type: ev.target.data("type"), target: ev.target.data("target") }));
    cy.current = instance;
    return () => { instance.destroy(); cy.current = null; };
  }, [data, layout]);

  useEffect(() => {
    const c = cy.current;
    if (!c) return;
    c.elements().removeClass("sel hl");
    if (selected) c.getElementById(selected).addClass("sel");
    highlight?.forEach((id) => c.getElementById(id).addClass("hl"));
  }, [selected, highlight, data]);

  return <div ref={host} className="graph-canvas" role="img" aria-label={`Graph with ${data.nodes.length} nodes and ${data.edges.length} edges`} />;
}

export function Legend({ types }: { types: string[] }) {
  return (
    <div className="legend">
      {types.map((t) => <span key={t}><i style={{ background: colorOf(t) }} />{t}</span>)}
      <span className="muted">solid = detected · dashed = inferred · dotted = heuristic</span>
    </div>
  );
}
