// Knowledge — local Knowledge Bases + RAG retrieval (PRD-03, Milestone 1).
// Create a KB, drop in .txt/.md files (chunked + embedded locally), inspect
// ingested sources, and test retrieval with the "ask this KB" search box.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, apiJson, apiPost } from "@/lib/api";

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  embeddingModel: string;
  embedDim: number | null;
  createdAt: string;
  indexedAt: string | null;
  chunkCount: number;
  sourceCount: number;
}

interface Source {
  source: string;
  chunkCount: number;
  createdAt: string;
}

interface SearchHit {
  id: string;
  source: string;
  ordinal: number;
  text: string;
  score: number;
}

const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary";

export function KnowledgePage() {
  const [kbs, setKbs] = useState<KnowledgeBase[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadKbs = useCallback(async () => {
    try {
      const list = (await apiJson<{ knowledgeBases: KnowledgeBase[] }>("/api/kb")).knowledgeBases;
      setKbs(list);
      setSelectedId((cur) => (cur && list.some((k) => k.id === cur) ? cur : list[0]?.id ?? null));
      if (list.length === 0) setCreating(true);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { loadKbs(); }, [loadKbs]);

  const selected = useMemo(() => kbs.find((k) => k.id === selectedId) ?? null, [kbs, selectedId]);

  return (
    <div className="flex h-full min-h-0">
      {/* KB list */}
      <div className="flex w-[260px] shrink-0 flex-col border-r border-pressed bg-softer">
        <div className="flex items-center justify-between px-4 py-4">
          <h2 className="font-display text-lg font-semibold text-ink">Knowledge</h2>
          <button onClick={() => { setCreating(true); setSelectedId(null); }} title="New knowledge base"
            className="rounded-lg bg-primary px-2.5 py-1 text-xs font-semibold text-white hover:bg-primary-deep">+ New</button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
          {kbs.length === 0 && !creating && (
            <p className="px-2 py-3 text-xs text-mute">No knowledge bases yet.</p>
          )}
          {kbs.map((k) => {
            const on = !creating && k.id === selectedId;
            return (
              <button key={k.id} onClick={() => { setCreating(false); setSelectedId(k.id); }}
                className={`mb-1 block w-full rounded-xl px-3 py-2 text-left transition-colors ${
                  on ? "bg-primary-soft text-primary-deep" : "text-body hover:bg-soft hover:text-ink"
                }`}>
                <div className="truncate text-sm font-bold">{k.name}</div>
                <div className="mt-0.5 text-[11px] text-mute">{k.sourceCount} source(s) · {k.chunkCount} chunk(s)</div>
              </button>
            );
          })}
          {creating && (
            <div className="mb-1 rounded-xl bg-soft px-3 py-2 text-sm font-semibold text-ink">New knowledge base…</div>
          )}
        </div>
      </div>

      {/* Detail */}
      <div className="min-w-0 flex-1 overflow-y-auto">
        {error && <div className="m-4 rounded-xl border border-danger bg-canvas px-3 py-2 text-xs text-danger">{error}</div>}
        {creating ? (
          <CreateKb onCreated={async () => { setCreating(false); await loadKbs(); }} onCancel={() => { setCreating(false); }} />
        ) : selected ? (
          <KbDetail kb={selected} onChanged={loadKbs} onDeleted={async () => { setSelectedId(null); await loadKbs(); }} />
        ) : (
          <div className="grid h-full place-items-center text-sm text-mute">Select or create a knowledge base.</div>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Create
// --------------------------------------------------------------------------- //

function CreateKb({ onCreated, onCancel }: { onCreated: () => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const create = async () => {
    if (!name.trim()) { setError("Give the knowledge base a name."); return; }
    setBusy(true); setError(null);
    try {
      await apiPost("/api/kb", { name: name.trim(), description: description.trim() });
      onCreated();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="mx-auto max-w-2xl px-6 py-8">
      <h2 className="font-display text-2xl font-semibold text-ink">New knowledge base</h2>
      <p className="mt-1 text-sm text-mute">
        A local collection of your documents. Files are chunked and embedded on this machine
        (via your active local model) — nothing is uploaded to the cloud.
      </p>
      <div className="mt-6 space-y-4 rounded-2xl border border-line bg-canvas p-5">
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Name</label>
          <input className={inputCls} value={name} onChange={(e) => setName(e.target.value)}
            placeholder="e.g. Company Handbook" autoFocus />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Description (optional)</label>
          <input className={inputCls} value={description} onChange={(e) => setDescription(e.target.value)}
            placeholder="What's in this knowledge base?" />
        </div>
        {error && <p className="text-xs text-danger">{error}</p>}
        <div className="flex gap-2">
          <button onClick={create} disabled={busy}
            className="rounded-xl bg-primary px-5 py-2.5 text-sm font-semibold text-white hover:bg-primary-deep disabled:opacity-40">
            {busy ? "Creating…" : "Create"}
          </button>
          <button onClick={onCancel}
            className="rounded-xl border border-pressed px-5 py-2.5 text-sm text-body hover:border-ink hover:text-ink">Cancel</button>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Detail — sources, upload, test search
// --------------------------------------------------------------------------- //

function KbDetail({ kb, onChanged, onDeleted }: {
  kb: KnowledgeBase; onChanged: () => void; onDeleted: () => void;
}) {
  const [sources, setSources] = useState<Source[]>([]);
  const [uploading, setUploading] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loadSources = useCallback(async () => {
    try {
      const r = await apiJson<{ sources: Source[] }>(`/api/kb/${encodeURIComponent(kb.id)}`);
      setSources(r.sources);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, [kb.id]);
  useEffect(() => { loadSources(); }, [loadSources]);

  const upload = useCallback(async (files: FileList | File[]) => {
    const list = Array.from(files).filter((f) => /\.(txt|md|markdown|text)$/i.test(f.name));
    if (list.length === 0) { setError("Only .txt and .md files are supported (PDF/docx is coming)."); return; }
    setUploading(true); setError(null);
    try {
      for (const f of list) {
        const form = new FormData();
        form.append("file", f);
        const res = await apiFetch(`/api/kb/${encodeURIComponent(kb.id)}/sources`, { method: "POST", body: form });
        if (!res.ok) {
          const t = await res.text();
          let msg = `HTTP ${res.status}`;
          try { msg = JSON.parse(t)?.detail ?? msg; } catch { /* */ }
          throw new Error(`${f.name}: ${msg}`);
        }
      }
      await loadSources(); onChanged();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setUploading(false); }
  }, [kb.id, loadSources, onChanged]);

  const removeSource = async (source: string) => {
    try {
      await apiJson(`/api/kb/${encodeURIComponent(kb.id)}/sources/${encodeURIComponent(source)}`, { method: "DELETE" });
      await loadSources(); onChanged();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const removeKb = async () => {
    if (!confirm(`Delete "${kb.name}" and all its documents?`)) return;
    try { await apiJson(`/api/kb/${encodeURIComponent(kb.id)}`, { method: "DELETE" }); onDeleted(); }
    catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 className="truncate font-display text-2xl font-semibold text-ink">{kb.name}</h2>
          {kb.description && <p className="mt-1 text-sm text-mute">{kb.description}</p>}
          <p className="mt-1 text-xs text-mute">
            {kb.sourceCount} source(s) · {kb.chunkCount} chunk(s)
            {kb.embeddingModel ? ` · model ${kb.embeddingModel}` : ""}
            {kb.embedDim ? ` · dim ${kb.embedDim}` : ""}
          </p>
        </div>
        <button onClick={removeKb}
          className="shrink-0 rounded-xl border border-pressed px-3 py-1.5 text-xs text-mute hover:border-danger hover:text-danger">Delete KB</button>
      </div>

      {error && <div className="mt-4 rounded-xl border border-danger bg-canvas px-3 py-2 text-xs text-danger">{error}</div>}

      {/* Upload dropzone */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => { e.preventDefault(); setDragOver(false); upload(e.dataTransfer.files); }}
        onClick={() => fileRef.current?.click()}
        className={`mt-6 cursor-pointer rounded-2xl border-2 border-dashed px-6 py-10 text-center transition-colors ${
          dragOver ? "border-primary bg-primary-soft" : "border-pressed bg-softer hover:border-primary-line"
        }`}>
        <input ref={fileRef} type="file" multiple accept=".txt,.md,.markdown,.text" className="hidden"
          onChange={(e) => { if (e.target.files) upload(e.target.files); e.currentTarget.value = ""; }} />
        <p className="text-sm font-semibold text-ink">{uploading ? "Ingesting…" : "Drop .txt / .md files here"}</p>
        <p className="mt-1 text-xs text-mute">or click to choose · embedded locally · PDF/docx coming soon</p>
      </div>

      {/* Sources */}
      <div className="mt-6">
        <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">Sources</h3>
        {sources.length === 0 ? (
          <p className="mt-2 text-sm text-mute">No documents ingested yet.</p>
        ) : (
          <div className="mt-2 space-y-1.5">
            {sources.map((s) => (
              <div key={s.source} className="group flex items-center gap-2 rounded-xl border border-line bg-canvas px-3 py-2">
                <span className="text-sm">📄</span>
                <span className="min-w-0 flex-1 truncate text-sm text-ink" title={s.source}>{s.source}</span>
                <span className="shrink-0 text-xs text-mute">{s.chunkCount} chunk(s)</span>
                <button onClick={() => removeSource(s.source)} title="Remove"
                  className="shrink-0 text-mute opacity-0 hover:text-danger group-hover:opacity-100">✕</button>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Test search */}
      <TestSearch kbId={kb.id} />
    </div>
  );
}

function TestSearch({ kbId }: { kbId: string }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    if (!query.trim()) return;
    setBusy(true); setError(null);
    try {
      const r = await apiPost<{ results: SearchHit[] }>(`/api/kb/${encodeURIComponent(kbId)}/search`, { query: query.trim(), k: 5 });
      setHits(r.results);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); setHits(null); }
    finally { setBusy(false); }
  };

  return (
    <div className="mt-8 rounded-2xl border border-line bg-canvas p-5">
      <h3 className="text-[11px] font-extrabold uppercase tracking-wide text-mute">Ask this knowledge base</h3>
      <p className="mt-1 text-xs text-mute">Test retrieval before attaching the KB to an agent.</p>
      <div className="mt-3 flex gap-2">
        <input className={`${inputCls} flex-1`} value={query} onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); run(); } }}
          placeholder="e.g. what is the refund window?" />
        <button onClick={run} disabled={busy || !query.trim()}
          className="shrink-0 rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white hover:bg-primary-deep disabled:opacity-40">
          {busy ? "Searching…" : "Search"}
        </button>
      </div>
      {error && <p className="mt-3 text-xs text-danger">{error}</p>}
      {hits && hits.length === 0 && <p className="mt-3 text-sm text-mute">No matching passages.</p>}
      {hits && hits.length > 0 && (
        <div className="mt-4 space-y-2">
          {hits.map((h) => (
            <div key={h.id} className="rounded-xl border border-line bg-softer p-3">
              <div className="mb-1 flex items-center gap-2 text-[11px] text-mute">
                <span className="font-mono">📄 {h.source}</span>
                <span className="ml-auto rounded-full bg-primary-soft px-2 py-0.5 font-semibold text-primary-deep">{h.score.toFixed(3)}</span>
              </div>
              <p className="whitespace-pre-wrap text-xs leading-relaxed text-body">{h.text}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default KnowledgePage;
