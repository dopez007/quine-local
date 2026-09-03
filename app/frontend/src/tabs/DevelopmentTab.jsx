import React, { useEffect, useState } from "react";
import { appGet, apiUrl } from "../api.js";
import { Card, Button, Empty, Pre, Spinner } from "../components";
import { useToast } from "../components/Toast.jsx";

// A window into the development/ sandbox — the workspace where the agent builds arbitrary
// software (via its dev_* tools). Browse the file tree, read a file, and download the whole
// thing as a .zip (take-out). Backend:
//   GET /api/development                  → { root, tree }
//   GET /api/development/files/{path}     → { path, content }
//   GET /api/development/export           → development.zip (Content-Disposition)
function TreeNode({ node, depth, onOpen, active }) {
  const [open, setOpen] = useState(depth < 1);
  const pad = { paddingLeft: 8 + depth * 14 };
  if (node.kind === "dir") {
    return (
      <div>
        <div className="tree-row" style={pad} onClick={() => setOpen((o) => !o)}>
          <span className="tree-twisty">{open ? "▾" : "▸"}</span>
          <span className="tree-name">{node.name.split("/").pop()}/</span>
        </div>
        {open &&
          (node.children || []).map((c) => (
            <TreeNode key={c.name} node={c} depth={depth + 1} onOpen={onOpen} active={active} />
          ))}
      </div>
    );
  }
  return (
    <div
      className={"tree-row file" + (active === node.name ? " active" : "")}
      style={pad}
      onClick={() => onOpen(node.name)}
    >
      <span className="tree-name">{node.name.split("/").pop()}</span>
      <span className="muted mono tree-size">{node.size}b</span>
    </div>
  );
}

export default function DevelopmentTab() {
  const [tree, setTree] = useState(null);
  const [loading, setLoading] = useState(true);
  const [activePath, setActivePath] = useState(null);
  const [content, setContent] = useState("");
  const [reading, setReading] = useState(false);
  const toast = useToast();

  async function load() {
    setLoading(true);
    try {
      const data = await appGet("/api/development");
      setTree(data.tree || []);
    } catch {
      toast.err("Could not load the development workspace.");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function open(path) {
    setActivePath(path);
    setReading(true);
    try {
      const f = await appGet("/api/development/files/" + path.split("/").map(encodeURIComponent).join("/"));
      setContent(f.content ?? f.error ?? "");
    } catch {
      setContent("(could not read file)");
    } finally {
      setReading(false);
    }
  }

  const empty = !tree || tree.length === 0;

  return (
    <Card
      title="Development sandbox"
      actions={
        <>
          {/* A plain link so the browser downloads via Content-Disposition. */}
          <a className="btn primary" href={apiUrl("/api/development/export")}>
            Download .zip
          </a>
          <Button onClick={load}>Refresh</Button>
        </>
      }
    >
      <p className="muted" style={{ marginTop: 0 }}>
        This is the workspace where the agent builds software for you (using its{" "}
        <code>dev_*</code> tools in the Run tab). Browse what it created, and download the whole
        project as a zip to take it with you.
      </p>

      {loading ? (
        <Empty>Loading…</Empty>
      ) : empty ? (
        <Empty>
          Nothing here yet. Ask the agent in the Run tab to build something (e.g. “build a small
          Python CLI in the sandbox”), then come back to browse and download it.
        </Empty>
      ) : (
        <div className="dev-split">
          <div className="dev-tree">
            {tree.map((n) => (
              <TreeNode key={n.name} node={n} depth={0} onOpen={open} active={activePath} />
            ))}
          </div>
          <div className="dev-viewer">
            {activePath ? (
              <>
                <div className="dev-viewer-head">
                  <span className="mono">{activePath}</span>
                  {reading && <Spinner />}
                </div>
                <Pre className="log">{content}</Pre>
              </>
            ) : (
              <Empty>Select a file to view it.</Empty>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
