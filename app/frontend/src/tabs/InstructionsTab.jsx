import React, { useEffect, useState, lazy, Suspense } from "react";
import { appGet, appPut, appDelete } from "../api.js";
import { Card, Button, Badge, Empty, Field, TextInput, TextArea, Spinner } from "../components";
import { useToast } from "../components/Toast.jsx";
import { useConfirm } from "../components/Confirm.jsx";

// The in-app manual: a guide per tab/feature, written for the user. Pages are shipped with
// the app and edited/added by the user; the agent keeps them in sync as features change.
// Backend:
//   GET    /api/instructions          → { documents: [{slug,title,category,source,...}] }
//   GET    /api/instructions/{slug}   → full doc with markdown content
//   PUT    /api/instructions/{slug}   → save the user's version (override)
//   DELETE /api/instructions/{slug}   → reset to the shipped version (or delete if custom)
const Markdown = lazy(() => import("../components/Markdown.jsx"));
function Md({ children }) {
  if (!children) return null;
  return (
    <Suspense fallback={<pre className="pre">{children}</pre>}>
      <Markdown>{children}</Markdown>
    </Suspense>
  );
}

const SOURCE = {
  shipped: { tone: "", label: "shipped" },
  edited: { tone: "accent", label: "edited" },
  custom: { tone: "good", label: "custom" },
};

function slugify(t) {
  return (t || "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "untitled";
}

export default function InstructionsTab() {
  const [docs, setDocs] = useState([]);
  const [slug, setSlug] = useState(null);
  const [doc, setDoc] = useState(null);
  const [loading, setLoading] = useState(true);
  const [reading, setReading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(null); // { slug, title, category, content, isNew }
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();

  async function openDoc(s) {
    setSlug(s);
    setEditing(false);
    setDraft(null);
    setReading(true);
    try {
      setDoc(await appGet("/api/instructions/" + encodeURIComponent(s)));
    } catch {
      setDoc(null);
    } finally {
      setReading(false);
    }
  }

  async function loadList(select) {
    setLoading(true);
    try {
      const { documents } = await appGet("/api/instructions");
      setDocs(documents || []);
      const next = select || slug || documents?.[0]?.slug;
      if (next) await openDoc(next);
      else setDoc(null);
    } catch {
      toast.err("Could not load the instructions.");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    loadList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function startEdit() {
    if (!doc) return;
    setDraft({ slug: doc.slug, title: doc.title, category: doc.category, content: doc.content, isNew: false });
    setEditing(true);
  }
  function startNew() {
    setDraft({ slug: "", title: "", category: "General", content: "# New page\n\nWrite the guide here…\n", isNew: true });
    setEditing(true);
  }

  async function save() {
    const key = draft.isNew ? slugify(draft.title) : draft.slug;
    if (draft.isNew && !draft.title.trim()) return toast.err("A title is required.");
    if (!draft.content.trim()) return toast.err("Content is required.");
    setBusy(true);
    const r = await appPut("/api/instructions/" + encodeURIComponent(key), {
      title: draft.title || draft.slug,
      category: draft.category,
      content: draft.content,
    });
    setBusy(false);
    if (r.error) return toast.err(r.error);
    toast.ok("Saved.");
    setEditing(false);
    setDraft(null);
    setSlug(r.document?.slug || key);
    await loadList(r.document?.slug || key);
  }

  async function resetDoc() {
    const isEdited = doc.source === "edited";
    const ok = await confirm({
      title: isEdited ? "Reset to default" : "Delete page",
      body: isEdited
        ? `Discard your edits to "${doc.title}" and restore the version it shipped with?`
        : `Delete "${doc.title}"? This page isn't part of the app's defaults, so it won't come back.`,
      danger: !isEdited,
      confirmLabel: isEdited ? "Reset" : "Delete",
    });
    if (!ok) return;
    setBusy(true);
    const r = await appDelete("/api/instructions/" + encodeURIComponent(doc.slug));
    setBusy(false);
    toast.ok(r.reverted ? "Reverted to the shipped version." : "Deleted.");
    await loadList(r.reverted ? doc.slug : null);
  }

  // Group docs by category, preserving the server's order.
  const groups = [];
  for (const d of docs) {
    let g = groups.find((x) => x.category === d.category);
    if (!g) groups.push((g = { category: d.category, items: [] }));
    g.items.push(d);
  }

  return (
    <Card
      title="Instructions"
      actions={
        <>
          <Button variant="primary" onClick={startNew} disabled={busy}>New page</Button>
          <Button onClick={() => loadList()} disabled={busy}>Refresh</Button>
        </>
      }
    >
      <p className="muted" style={{ marginTop: 0 }}>
        Your manual for the app — a short guide for each tab and feature. Edit any page to add
        your own notes, or add your own. New guides appear here automatically as the app gains
        features.
      </p>

      {loading ? (
        <Empty>Loading…</Empty>
      ) : docs.length === 0 && !editing ? (
        <Empty>No instructions yet. Click “New page” to write the first one.</Empty>
      ) : (
        <div className="dev-split">
          <div className="dev-tree ins-list">
            {groups.map((g) => (
              <div key={g.category} className="ins-group">
                <div className="ins-group-head">{g.category}</div>
                {g.items.map((d) => (
                  <div
                    key={d.slug}
                    className={"tree-row file" + (slug === d.slug && !editing ? " active" : "")}
                    onClick={() => openDoc(d.slug)}
                    title={d.summary}
                  >
                    <span className="tree-name">{d.title}</span>
                    {d.source !== "shipped" && (
                      <span className={"badge " + SOURCE[d.source].tone}>{SOURCE[d.source].label}</span>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </div>

          <div className="dev-viewer ins-doc">
            {editing && draft ? (
              <div className="ins-edit">
                <div className="ins-doc-head">
                  <h3 className="ins-title">{draft.isNew ? "New page" : `Editing: ${draft.title}`}</h3>
                  <div className="row">
                    <Button variant="primary" onClick={save} disabled={busy}>
                      {busy ? "Saving…" : "Save"}
                    </Button>
                    <Button onClick={() => { setEditing(false); setDraft(null); }} disabled={busy}>
                      Cancel
                    </Button>
                  </div>
                </div>
                {draft.isNew && (
                  <Field label="Title">
                    <TextInput
                      value={draft.title}
                      onChange={(e) => setDraft({ ...draft, title: e.target.value })}
                      placeholder="e.g. Kanban board"
                      autoFocus
                    />
                  </Field>
                )}
                <Field label="Category" hint="Pages are grouped under this heading in the list.">
                  <TextInput
                    value={draft.category}
                    onChange={(e) => setDraft({ ...draft, category: e.target.value })}
                    placeholder="General"
                  />
                </Field>
                <Field label="Content (Markdown)">
                  <TextArea
                    rows={18}
                    value={draft.content}
                    onChange={(e) => setDraft({ ...draft, content: e.target.value })}
                    spellCheck={false}
                    style={{ fontFamily: "var(--mono, monospace)" }}
                  />
                </Field>
              </div>
            ) : reading ? (
              <Spinner label="Loading…" />
            ) : doc ? (
              <>
                <div className="ins-doc-head">
                  <div>
                    <h3 className="ins-title">{doc.title}</h3>
                    <span className="muted small">{doc.category}</span>
                  </div>
                  <div className="row">
                    <Badge tone={SOURCE[doc.source]?.tone}>{SOURCE[doc.source]?.label}</Badge>
                    <Button onClick={startEdit} disabled={busy}>Edit</Button>
                    {doc.source !== "shipped" && (
                      <Button variant="danger" onClick={resetDoc} disabled={busy}>
                        {doc.source === "edited" ? "Reset" : "Delete"}
                      </Button>
                    )}
                  </div>
                </div>
                <div className="msg-body ins-body">
                  <Md>{doc.content}</Md>
                </div>
              </>
            ) : (
              <Empty>Select a page to read it.</Empty>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
