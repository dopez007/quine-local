import React, { useEffect, useRef, useState } from "react";
import { appGet, appDelete, appPost, appUpload } from "../api.js";
import { Card, Button, Badge, Empty, Field, TextInput, TextArea } from "../components";
import { useToast } from "../components/Toast.jsx";
import { useConfirm } from "../components/Confirm.jsx";

// Bring-your-own-data: upload documents the Run agent can answer over via its
// search_knowledge tool. Backend:
//   GET    /api/knowledge
//   POST   /api/knowledge/upload   (multipart file)
//   POST   /api/knowledge/text     {title, content}
//   DELETE /api/knowledge/{title}
export default function KnowledgeTab() {
  const [docs, setDocs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [showPaste, setShowPaste] = useState(false);
  const [paste, setPaste] = useState({ title: "", content: "" });
  const fileRef = useRef(null);
  const toast = useToast();
  const confirm = useConfirm();

  async function load() {
    setLoading(true);
    try {
      const { documents } = await appGet("/api/knowledge");
      setDocs(documents || []);
    } catch {
      toast.err("Could not load documents.");
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function onPickFile(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    const d = await appUpload("/api/knowledge/upload", file);
    if (d.error) toast.err(d.error);
    else toast.ok(`Added "${d.document?.title}" (${d.document?.chunks} chunks).`);
    if (fileRef.current) fileRef.current.value = "";
    await load();
    setBusy(false);
  }

  async function addPaste(e) {
    e.preventDefault();
    if (!paste.title.trim() || !paste.content.trim()) return;
    setBusy(true);
    const d = await appPost("/api/knowledge/text", paste);
    if (d.error) toast.err(d.error);
    else {
      toast.ok(`Added "${d.document?.title}" (${d.document?.chunks} chunks).`);
      setPaste({ title: "", content: "" });
      setShowPaste(false);
    }
    await load();
    setBusy(false);
  }

  async function remove(doc) {
    const ok = await confirm({
      title: "Delete document",
      body: `Delete "${doc.title}" from the knowledge base?`,
      danger: true,
      confirmLabel: "Delete",
    });
    if (!ok) return;
    setBusy(true);
    await appDelete("/api/knowledge/" + encodeURIComponent(doc.title));
    toast.ok(`Deleted "${doc.title}".`);
    await load();
    setBusy(false);
  }

  return (
    <Card
      title="Knowledge"
      actions={
        <>
          <Button variant="primary" onClick={() => fileRef.current?.click()} disabled={busy}>
            {busy ? "Working…" : "Upload file"}
          </Button>
          <Button onClick={() => setShowPaste((s) => !s)}>{showPaste ? "Cancel" : "Paste text"}</Button>
          <Button onClick={load}>Refresh</Button>
        </>
      }
    >
      <input
        ref={fileRef}
        type="file"
        accept=".txt,.md,.csv,.json,.py,.js,.ts,.html,.pdf,.log,.yaml,.yml"
        style={{ display: "none" }}
        onChange={onPickFile}
      />
      <p className="muted" style={{ marginTop: 0 }}>
        Upload documents (text, markdown, CSV, code, or PDF). The Run agent searches them with
        its <code>search_knowledge</code> tool to answer questions about your data. Files are
        chunked and stored under the data partition, so they survive reboots.
      </p>

      {showPaste && (
        <form className="install-form" onSubmit={addPaste}>
          <Field label="Title">
            <TextInput
              value={paste.title}
              onChange={(e) => setPaste({ ...paste, title: e.target.value })}
              placeholder="meeting-notes"
              autoFocus
            />
          </Field>
          <Field label="Content">
            <TextArea
              rows={8}
              value={paste.content}
              onChange={(e) => setPaste({ ...paste, content: e.target.value })}
              placeholder="Paste any text to make it searchable…"
              spellCheck={false}
            />
          </Field>
          <div className="row">
            <Button variant="primary" type="submit" disabled={busy}>
              Add to knowledge
            </Button>
          </div>
        </form>
      )}

      {loading ? (
        <Empty>Loading…</Empty>
      ) : docs.length === 0 ? (
        <Empty>No documents yet. Upload a file or paste text to get started.</Empty>
      ) : (
        <table className="table">
          <thead>
            <tr>
              <th>Document</th>
              <th>Chunks</th>
              <th>Chars</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.title}>
                <td>
                  <strong>{d.title}</strong>
                </td>
                <td>
                  <Badge tone="accent">{d.chunks}</Badge>
                </td>
                <td className="muted mono">{d.chars.toLocaleString()}</td>
                <td className="nowrap">
                  <Button variant="danger" onClick={() => remove(d)} disabled={busy}>
                    Delete
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
