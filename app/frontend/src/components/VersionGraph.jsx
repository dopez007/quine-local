import React, { useCallback, useEffect, useMemo, useRef } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  Handle,
  Position,
  useReactFlow,
  useNodesInitialized,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { Badge } from "./index.jsx";
import { STATUS_TONES, toneOf } from "./versionMeta.jsx";

// Animated git-graph view of the version lineage. The backend already returns a real
// DAG per version (parent = first-parent tree edge; reverts/reapplies = cross-edges;
// on_main = the active trunk), so this lays every version out on a lane and draws:
//   • solid edges  parent → child (the tree)
//   • dashed edges revert / re-apply (the relationships)
// Selection + all mutating actions live in the parent (VersionsTab) — this component
// only visualizes and reports clicks via onSelect(v). Version *source* is never shown.

const ROW_H = 96; // vertical spacing between successive versions
const LANE_W = 240; // horizontal spacing between branch lanes
const PAD = 24;

const ORIGIN_GLYPH = {
  seed: "🌱",
  "self-mod": "✎",
  revert: "⎌",
  reapply: "⤴",
  backfill: "•",
};

// Resolve a CSS custom property to a concrete color (MiniMap paints to canvas, where
// CSS vars don't cascade). Recomputed per render so it follows the light/dark theme.
function cssVar(name, fallback) {
  if (typeof window === "undefined") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

// GitHub-style lane packing. Versions come newest-first; row index → y (newest on top).
// Trunk (on_main) is always lane 0; each divergent off-main branch takes the next free
// lane, released back to the pool when its chain terminates at the trunk. Branches here
// are shallow (a rolled-back tip + its abandoned successors), so width stays small.
function computeLayout(versions) {
  const rows = [...versions].sort((a, b) => (b.seq || 0) - (a.seq || 0));
  const bySha = new Map(rows.map((v) => [v.sha, v]));
  const reserved = new Map(); // sha (a parent) → lane a child already claimed for it
  const free = []; // freed off-main lanes, reused to keep the graph narrow
  let maxLane = 0; // highest off-main lane in use (0 is the trunk)
  const laneOf = new Map();

  const alloc = () => {
    if (free.length) {
      free.sort((a, b) => a - b);
      return free.shift();
    }
    return ++maxLane;
  };

  rows.forEach((v) => {
    let lane;
    if (v.on_main) lane = 0;
    else if (reserved.has(v.sha)) {
      lane = reserved.get(v.sha);
      reserved.delete(v.sha);
    } else lane = alloc();
    laneOf.set(v.sha, lane);

    // Propagate the lane to the parent so an off-main chain stays in one column.
    if (v.parent && !v.on_main) {
      const parent = bySha.get(v.parent);
      if (parent && !parent.on_main && !reserved.has(v.parent)) {
        reserved.set(v.parent, lane); // chain continues into the parent
      } else if (lane !== 0) {
        free.push(lane); // parent is on the trunk (or already claimed) → chain ends here
      }
    }
  });

  const pos = new Map();
  rows.forEach((v, i) => {
    pos.set(v.sha, { x: PAD + laneOf.get(v.sha) * LANE_W, y: PAD + i * ROW_H, row: i });
  });
  return { rows, pos, bySha, lanes: maxLane + 1 };
}

// One version card. Four handles: top/bottom carry the tree edges (to children above /
// parent below); the right pair carry the dashed revert/re-apply cross-edges.
function VersionNode({ data }) {
  const { v, isActive, isSelected } = data;
  const tone = toneOf(v, isActive);
  const cls = [
    "vg-node",
    v.on_main ? "on-main" : "off-main",
    isActive ? "is-active" : "",
    isSelected ? "is-selected" : "",
    data.isPending ? "is-pending" : "",
    tone ? "tone-" + tone : "",
  ].join(" ");
  return (
    <div className={cls} style={{ "--i": data.data_row }}>
      <Handle type="target" position={Position.Top} id="t" isConnectable={false} />
      <Handle type="source" position={Position.Bottom} id="b" isConnectable={false} />
      <Handle type="target" position={Position.Right} id="rt" isConnectable={false} />
      <Handle type="source" position={Position.Right} id="rs" isConnectable={false} />
      <div className="vg-node-head">
        <strong className="vg-seq">{v.seq ? "v" + v.seq : "—"}</strong>
        {isActive ? (
          <Badge tone="accent">active</Badge>
        ) : v.status ? (
          <Badge tone={STATUS_TONES[v.status] || ""}>{v.status.replace("_", " ")}</Badge>
        ) : null}
      </div>
      <div className="vg-node-sub">
        <code>{v.short}</code>
        {v.label && <span className="vg-label" title={v.label}>🏷 {v.label}</span>}
      </div>
      <div className="vg-node-meta muted">
        <span title={v.origin || ""}>{ORIGIN_GLYPH[v.origin] || "•"}</span>
        <span>{(v.date || "").replace("T", " ").slice(0, 16)}</span>
      </div>
    </div>
  );
}

const nodeTypes = { version: VersionNode };

// Custom nodes are measured only after their first paint, so a fitView on <ReactFlow>
// mount frames the wrong (unmeasured) bounds and leaves the graph off-screen. Fit once
// nodes are measured, and re-fit whenever the node set changes (an action added one).
function AutoFit({ fitKey }) {
  const initialized = useNodesInitialized();
  const { fitView } = useReactFlow();
  useEffect(() => {
    if (!initialized) return;
    const t = setTimeout(() => fitView({ padding: 0.3, maxZoom: 1.2, duration: 350 }), 30);
    return () => clearTimeout(t);
  }, [initialized, fitKey, fitView]);
  return null;
}

export default function VersionGraph({ versions, activeVersion, pendingSet, selectedSha, onSelect }) {
  const rfRef = useRef(null);

  const { nodes, edges } = useMemo(() => {
    const { rows, pos, bySha } = computeLayout(versions);
    const nodes = rows.map((v) => ({
      id: v.sha,
      type: "version",
      position: { x: pos.get(v.sha).x, y: pos.get(v.sha).y },
      data: {
        v,
        data_row: pos.get(v.sha).row,
        isActive: v.sha === activeVersion,
        isSelected: v.sha === selectedSha,
        isPending: pendingSet?.has(v.sha),
      },
      draggable: false,
      selectable: true,
    }));

    const edges = [];
    for (const v of rows) {
      // tree edge: this version (top) down to its parent (below)
      if (v.parent && bySha.has(v.parent)) {
        edges.push({
          id: `t-${v.sha}`,
          source: v.sha,
          target: v.parent,
          sourceHandle: "b",
          targetHandle: "t",
          type: "smoothstep",
          className: "vg-tree-edge" + (v.on_main ? " on-main" : ""),
        });
      }
      // cross edges: revert / re-apply relationships
      if (v.reverts && bySha.has(v.reverts)) {
        edges.push({
          id: `rv-${v.sha}`,
          source: v.sha,
          target: v.reverts,
          sourceHandle: "rs",
          targetHandle: "rt",
          type: "default",
          animated: true,
          className: "vg-cross-edge revert",
        });
      }
      if (v.reapplies && bySha.has(v.reapplies)) {
        edges.push({
          id: `ra-${v.sha}`,
          source: v.sha,
          target: v.reapplies,
          sourceHandle: "rs",
          targetHandle: "rt",
          type: "default",
          animated: true,
          className: "vg-cross-edge reapply",
        });
      }
    }
    return { nodes, edges };
  }, [versions, activeVersion, selectedSha, pendingSet]);

  const onNodeClick = useCallback((_e, node) => onSelect?.(node.data.v), [onSelect]);

  const jumpTo = useCallback(
    (sha) => {
      if (rfRef.current && sha) {
        rfRef.current.fitView({ nodes: [{ id: sha }], duration: 500, padding: 0.6, maxZoom: 1.3 });
      }
    },
    [],
  );

  const minimapColor = useCallback(
    (node) => {
      if (node.data?.isActive) return cssVar("--accent", "#3b82f6");
      if (!node.data?.v?.on_main) return cssVar("--muted", "#8899aa");
      return cssVar("--border", "#283142");
    },
    [],
  );

  return (
    <div className="vg-canvas">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onInit={(inst) => (rfRef.current = inst)}
        onNodeClick={onNodeClick}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        selectNodesOnDrag={false}
        minZoom={0.2}
        maxZoom={1.75}
        proOptions={{ hideAttribution: true }}
      >
        <AutoFit fitKey={`${nodes.length}:${activeVersion}`} />
        <Background gap={22} size={1.4} color={cssVar("--border", "#283142")} className="vg-bg" />
        <Controls showInteractive={false} />
        <MiniMap pannable zoomable nodeColor={minimapColor} className="vg-minimap" />
      </ReactFlow>
      <div className="vg-toolbar">
        <button
          className="btn"
          onClick={() => jumpTo(activeVersion)}
          disabled={!activeVersion}
          title="Recenter on the running version"
        >
          ◎ Active
        </button>
        <button
          className="btn"
          onClick={() => rfRef.current?.fitView({ padding: 0.3, duration: 500 })}
          title="Fit the whole tree"
        >
          ⤢ Fit
        </button>
      </div>
    </div>
  );
}
