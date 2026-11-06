// Wiki — LLM-maintained knowledge wikis in the Open Knowledge Format (PRD-13).
// Create/import a wiki, browse its OKF markdown pages + log, test retrieval,
// lint it (orphans/broken links/stale), and view the link graph. Agents fill it
// in via the wiki_* tools; export is a plain zip of the on-disk bundle.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch, apiJson, apiPost } from "@/lib/api";
import {
  MarkdownPreview, TierBadge, btnCls, inputCls,
  type Page, type SearchHit, type Wiki,
} from "./wiki-shared";
import { LinkRepository, LinkedWikiDetail } from "./linked-wiki-detail";
import { PendingProposals } from "./proposal-card";

interface Lint {
  orphans: string[];
  brokenLinks: { from: string; to: string }[];
  stale: { path: string; timestamp: string; ageDays: number }[];
  pageCount: number;
  staleDays: number;
}
interface Graph { nodes: string[]; edges: { from: string; to: string; resolved: boolean }[] }

type View = "search" | "page" | "lint" | "graph" | "log";

export function WikiPage() {
  const [wikis, setWikis] = useState<Wiki[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [linking, setLinking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const importRef = useRef<HTMLInputElement>(null);

  const load = useCallback(async () => {
    try {
      const list = (await apiJson<{ wikis: Wiki[] }>("/api/wiki")).wikis;
      setWikis(list);
      setSelectedId((cur) => (cur && list.some((w) => w.id === cur) ? cur : list[0]?.id ?? null));
      if (list.length === 0 && !linking) setCreating(true);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const onImport = async (file: File) => {
    setError(null);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const w = await apiJson<Wiki>("/api/wiki/import", { method: "POST", body: fd });
      await load();
      setCreating(false); setSelectedId(w.id);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const selected = useMemo(() => wikis.find((w) => w.id === selectedId) ?? null, [wikis, selectedId]);

  return (
    <div className="flex h-full min-h-0">
      <div className="flex w-[260px] shrink-0 flex-col border-r border-pressed bg-softer">
        <div className="flex items-center justify-between px-4 py-4">
          <h2 className="font-display text-lg font-semibold text-ink">Wiki</h2>
          <div className="flex gap-1">
            <button onClick={() => importRef.current?.click()} title="Import a wiki bundle or document"
              className="rounded-lg border border-pressed px-2 py-1 text-xs font-semibold text-body hover:text-ink">Import</button>
            <button onClick={() => { setLinking(true); setCreating(false); setSelectedId(null); }} title="Link a local Wiki repository"
              className="rounded-lg border border-pressed px-2 py-1 text-xs font-semibold text-body hover:text-ink">Link</button>
            <button onClick={() => { setCreating(true); setSelectedId(null); }} title="New wiki"
              className="rounded-lg bg-primary px-2.5 py-1 text-xs font-semibold text-white hover:bg-primary-deep">+ New</button>
          </div>
          <input ref={importRef} type="file" accept=".zip,.md,.markdown,.txt,.csv,.docx,.pptx,.xlsx" className="hidden"
            onChange={(e) => { const f = e.target.files?.[0]; if (f) onImport(f); e.target.value = ""; }} />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
          {wikis.length === 0 && !creating && <p className="px-2 py-3 text-xs text-mute">No wikis yet.</p>}
          {wikis.map((w) => {
            const on = !creating && w.id === selectedId;
            return (
              <button key={w.id} onClick={() => { setCreating(false); setSelectedId(w.id); }}
                className={`mb-1 block w-full rounded-xl px-3 py-2 text-left transition-colors ${
                  on ? "bg-primary-soft text-primary-deep" : "text-body hover:bg-soft hover:text-ink"
                }`}>
                <div className="truncate text-sm font-bold">{w.name}</div>
                <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-mute">
                  {w.sourceKind === "linked_repository" && (
                    <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
                      <path d="M3 3h13a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H3z" /><path d="M18 8h1a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-3" />
                    </svg>
                  )}
                  <span>{w.sourceKind === "linked_repository" ? "Linked · " : ""}{w.pageCount} page(s)</span>
                  {w.sourceKind === "linked_repository" && w.trustTier && <TierBadge tier={w.trustTier} mini />}
                </div>
              </button>
            );
          })}
          {creating && <div className="mb-1 rounded-xl bg-soft px-3 py-2 text-sm font-semibold text-ink">New wiki…</div>}
          {linking && <div className="mb-1 rounded-xl bg-soft px-3 py-2 text-sm font-semibold text-ink">Link repository…</div>}
        </div>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto">
        {error && <div className="m-4 rounded-xl border border-danger bg-canvas px-3 py-2 text-xs text-danger">{error}</div>}
        <div className="px-6 empty:px-0">
          <PendingProposals onResolved={async (p) => {
            await load();
            if (p.wikiId) { setCreating(false); setLinking(false); setSelectedId(p.wikiId); }
          }} />
        </div>
        {linking ? (
          <LinkRepository onLinked={async () => { setLinking(false); await load(); }} onCancel={() => setLinking(false)} />
        ) : creating ? (
          <CreateWiki onCreated={async () => { setCreating(false); await load(); }} onCancel={() => setCreating(false)} />
        ) : selected ? (
          selected.sourceKind === "linked_repository" ? <LinkedWikiDetail wiki={selected} onChanged={load} onDeleted={async () => { setSelectedId(null); await load(); }} /> :
            <WikiDetail wiki={selected} onChanged={load} onDeleted={async () => { setSelectedId(null); await load(); }} />
        ) : (
          <div className="grid h-full place-items-center text-sm text-mute">Select or create a wiki.</div>
        )}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //

function CreateWiki({ onCreated, onCancel }: { onCreated: () => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    if (!name.trim()) { setErr("Name is required."); return; }
    setBusy(true); setErr(null);
    try {
      await apiPost<Wiki>("/api/wiki", { name: name.trim(), description: description.trim() });
      onCreated();
    } catch (e) { setErr(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };

  return (
    <div className="mx-auto max-w-xl p-6">
      <h3 className="font-display text-xl font-semibold text-ink">New wiki</h3>
      <p className="mt-1 text-sm text-mute">A compounding knowledge base the agent maintains as OKF markdown pages.</p>
      <div className="mt-5 space-y-4">
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Name</label>
          <input className={inputCls} value={name} placeholder="e.g. Market research" autoFocus
            onChange={(e) => setName(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()} />
        </div>
        <div>
          <label className="mb-1 block text-xs font-medium text-body">Description</label>
          <input className={inputCls} value={description} placeholder="What this wiki is about"
            onChange={(e) => setDescription(e.target.value)} />
        </div>
        {err && <p className="text-xs text-danger">{err}</p>}
        <div className="flex gap-2">
          <button disabled={busy} onClick={submit}
            className="rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white hover:bg-primary-deep disabled:opacity-50">
            {busy ? "Creating…" : "Create wiki"}</button>
          <button onClick={onCancel} className="rounded-xl border border-pressed px-4 py-2 text-sm text-body hover:text-ink">Cancel</button>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //

function WikiDetail({ wiki, onChanged, onDeleted }: { wiki: Wiki; onChanged: () => void; onDeleted: () => void }) {
  const [pages, setPages] = useState<Page[]>([]);
  const [view, setView] = useState<View>("search");
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [preview, setPreview] = useState(true);
  const [lint, setLint] = useState<Lint | null>(null);
  const [graph, setGraph] = useState<Graph | null>(null);

  const loadPages = useCallback(async () => {
    const w = await apiJson<Wiki & { pages: Page[] }>(`/api/wiki/${wiki.id}`);
    setPages(w.pages);
  }, [wiki.id]);
  useEffect(() => { loadPages(); setView("search"); setOpenPath(null); setContent(""); setLint(null); setGraph(null); }, [loadPages]);

  const openPage = async (path: string) => {
    const r = await apiJson<{ content: string }>(`/api/wiki/${wiki.id}/pages/${path}`);
    setContent(r.content); setOpenPath(path); setView("page");
  };
  const runLint = async () => { setLint(await apiJson<Lint>(`/api/wiki/${wiki.id}/lint`)); setView("lint"); };
  const runGraph = async () => { setGraph(await apiJson<Graph>(`/api/wiki/${wiki.id}/graph`)); setView("graph"); };
  const openLog = async () => { setContent((await apiJson<{ log: string }>(`/api/wiki/${wiki.id}/log`)).log); setView("log"); };

  const delPage = async (path: string) => {
    if (!confirm(`Delete page "${path}"?`)) return;
    await apiJson(`/api/wiki/${wiki.id}/pages/${path}`, { method: "DELETE" });
    await loadPages(); onChanged(); setView("search"); setOpenPath(null);
  };
  const exportZip = async () => {
    const res = await apiFetch(`/api/wiki/${wiki.id}/export`);
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url; a.download = `${wiki.name.replace(/[^\w-]/g, "") || "wiki"}.zip`;
    a.click(); URL.revokeObjectURL(url);
  };
  const delWiki = async () => {
    if (!confirm(`Delete wiki "${wiki.name}" and all its pages? This can't be undone.`)) return;
    await apiJson(`/api/wiki/${wiki.id}`, { method: "DELETE" });
    onDeleted();
  };

  return (
    <div className="p-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h3 className="font-display text-xl font-semibold text-ink">{wiki.name}</h3>
          {wiki.description && <p className="mt-1 text-sm text-mute">{wiki.description}</p>}
          <p className="mt-1 text-xs text-mute">
            {pages.length} page(s){wiki.ingestedAt ? ` · last updated ${new Date(wiki.ingestedAt).toLocaleString()}` : " · empty"}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap justify-end gap-2">
          <button onClick={runLint} className={btnCls}>Lint</button>
          <button onClick={runGraph} className={btnCls}>Graph</button>
          <button onClick={openLog} className={btnCls}>Log</button>
          <button onClick={exportZip} className={btnCls}>Export</button>
          <button onClick={delWiki} className="rounded-xl border border-danger px-3 py-1.5 text-xs font-medium text-danger hover:bg-danger hover:text-white">Delete</button>
        </div>
      </div>

      <p className="mt-4 rounded-xl border border-pressed bg-softer px-3 py-2 text-xs text-mute">
        Filled in by agents using the <code className="rounded bg-soft px-1">wiki_*</code> tools — attach it to an agent
        (Agents → Wiki) and have it ingest sources. Pages are plain OKF markdown on disk.
      </p>

      <div className="mt-5 grid grid-cols-[minmax(0,260px)_minmax(0,1fr)] gap-4">
        <div className="rounded-2xl border border-pressed bg-canvas p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-semibold uppercase tracking-wide text-mute">Pages</span>
            <button onClick={() => { setView("search"); setOpenPath(null); }} className="text-[11px] text-primary hover:underline">search</button>
          </div>
          {pages.length === 0 ? (
            <p className="text-xs text-mute">No pages yet.</p>
          ) : (
            <div className="space-y-1">
              {pages.map((p) => (
                <button key={p.path} onClick={() => openPage(p.path)} title={p.description || p.path}
                  className={`block w-full truncate rounded-lg px-2 py-1.5 text-left text-xs transition-colors ${
                    view === "page" && openPath === p.path ? "bg-primary-soft text-primary-deep" : "text-body hover:bg-soft hover:text-ink"
                  }`}>
                  <span className="font-medium">{p.title}</span>{p.type && <span className="ml-1 text-mute">· {p.type}</span>}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="min-w-0">
          {view === "search" && <TestSearch wikiId={wiki.id} onOpenPage={openPage} />}
          {(view === "page" || view === "log") && (
            <div className="rounded-2xl border border-pressed bg-canvas p-4">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs font-medium text-mute">{view === "log" ? "log.md" : openPath}</span>
                <div className="flex items-center gap-3">
                  {view === "page" && (
                    <button onClick={() => setPreview((value) => !value)} className="text-[11px] text-primary hover:underline">
                      {preview ? "Markdown" : "Preview"}
                    </button>
                  )}
                  {view === "page" && openPath && (
                    <button onClick={() => delPage(openPath)} className="text-[11px] text-danger hover:underline">delete page</button>
                  )}
                </div>
              </div>
              {view === "page" && preview ? <MarkdownPreview content={content} /> : (
                <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-ink">{content}</pre>
              )}
            </div>
          )}
          {view === "lint" && lint && <LintView lint={lint} onOpenPage={openPage} />}
          {view === "graph" && graph && <GraphView graph={graph} pages={pages} onOpenPage={openPage} />}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //

function LintView({ lint, onOpenPage }: { lint: Lint; onOpenPage: (p: string) => void }) {
  const clean = lint.orphans.length === 0 && lint.brokenLinks.length === 0 && lint.stale.length === 0;
  return (
    <div className="space-y-3 rounded-2xl border border-pressed bg-canvas p-4">
      <div className="text-xs font-semibold uppercase tracking-wide text-mute">Lint · {lint.pageCount} page(s)</div>
      {clean && <p className="text-sm text-success">✓ No issues — every page is linked, links resolve, nothing is stale.</p>}
      <LintGroup title={`Orphans (${lint.orphans.length})`} hint="Pages no other page links to.">
        {lint.orphans.map((p) => (
          <button key={p} onClick={() => onOpenPage(p)} className="block text-left text-xs text-body hover:text-primary hover:underline">{p}</button>
        ))}
      </LintGroup>
      <LintGroup title={`Broken links (${lint.brokenLinks.length})`} hint="Links whose target page is missing.">
        {lint.brokenLinks.map((b, i) => (
          <button key={i} onClick={() => onOpenPage(b.from)} className="block text-left text-xs text-body hover:text-primary hover:underline">
            {b.from} → <span className="text-danger">{b.to}</span>
          </button>
        ))}
      </LintGroup>
      <LintGroup title={`Stale (${lint.stale.length})`} hint={`Not updated in over ${lint.staleDays} days.`}>
        {lint.stale.map((s) => (
          <button key={s.path} onClick={() => onOpenPage(s.path)} className="block text-left text-xs text-body hover:text-primary hover:underline">
            {s.path} <span className="text-mute">· {s.ageDays}d old</span>
          </button>
        ))}
      </LintGroup>
    </div>
  );
}

function LintGroup({ title, hint, children }: { title: string; hint: string; children: React.ReactNode }) {
  const arr = Array.isArray(children) ? children : [children];
  const empty = arr.filter(Boolean).length === 0;
  return (
    <div>
      <div className="text-xs font-semibold text-ink">{title}</div>
      <div className="text-[11px] text-mute">{hint}</div>
      <div className="mt-1 space-y-0.5">{empty ? <span className="text-xs text-mute">none</span> : children}</div>
    </div>
  );
}

// ponytail: layout is a plain force simulation run for a fixed number of
// iterations up front (no physics loop, no drag-drag re-simulate) — good
// enough for a few hundred nodes; swap for d3-force if graphs get huge.
function forceLayout(nodes: string[], edges: { from: string; to: string }[], width: number, height: number) {
  const pos = new Map<string, { x: number; y: number }>();
  nodes.forEach((n, i) => {
    const a = (i / Math.max(1, nodes.length)) * Math.PI * 2;
    pos.set(n, { x: width / 2 + Math.cos(a) * 150, y: height / 2 + Math.sin(a) * 150 });
  });
  const cx = width / 2, cy = height / 2;
  for (let iter = 0; iter < 300; iter++) {
    const disp = new Map<string, { x: number; y: number }>(nodes.map((n) => [n, { x: 0, y: 0 }]));
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = pos.get(nodes[i])!, b = pos.get(nodes[j])!;
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy || 0.01;
        const force = 2400 / d2;
        const d = Math.sqrt(d2);
        dx = (dx / d) * force; dy = (dy / d) * force;
        const da = disp.get(nodes[i])!, db = disp.get(nodes[j])!;
        da.x += dx; da.y += dy; db.x -= dx; db.y -= dy;
      }
    }
    for (const e of edges) {
      const a = pos.get(e.from), b = pos.get(e.to);
      if (!a || !b) continue;
      const dx = a.x - b.x, dy = a.y - b.y;
      const da = disp.get(e.from)!, db = disp.get(e.to)!;
      da.x -= dx * 0.02; da.y -= dy * 0.02;
      db.x += dx * 0.02; db.y += dy * 0.02;
    }
    for (const n of nodes) {
      const p = pos.get(n)!, d = disp.get(n)!;
      p.x += d.x - (p.x - cx) * 0.01;
      p.y += d.y - (p.y - cy) * 0.01;
      p.x = Math.max(30, Math.min(width - 30, p.x));
      p.y = Math.max(30, Math.min(height - 30, p.y));
    }
  }
  return pos;
}

// ponytail: hand-rolled categorical palette for the graph's color-by-type
// legend — the design tokens only give one brand hue, and viz series need
// distinct ones. Precedent: the broken-edge red below is already hard-coded.
const CAT_PALETTE = ["#2B3C82", "#c2410c", "#0f766e", "#7c3aed", "#b91c1c", "#0369a1", "#a16207", "#be185d", "#4d7c0f", "#475569"];

// Fit the laid-out graph into the viewport: pan+zoom so its bounding box is centered.
function fitView(layout: Map<string, { x: number; y: number }>, width: number, height: number) {
  const pts = [...layout.values()];
  if (pts.length === 0) return { x: 0, y: 0, k: 1 };
  const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
  const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
  const pad = 60;
  const k = Math.max(0.2, Math.min(1.5, Math.min(width / (maxX - minX + pad * 2), height / (maxY - minY + pad * 2))));
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  return { x: width / 2 - cx * k, y: height / 2 - cy * k, k };
}

function GraphView({ graph, pages, onOpenPage }: { graph: Graph; pages: Page[]; onOpenPage: (p: string) => void }) {
  const width = 900, height = 560;
  const layout = useMemo(() => forceLayout(graph.nodes, graph.edges, width, height), [graph]);
  const catOf = useMemo(() => {
    const m = new Map(pages.map((p) => [p.path, p.type || "Uncategorized"]));
    return (path: string) => m.get(path) || "Uncategorized";
  }, [pages]);
  const categories = useMemo(() => [...new Set(graph.nodes.map(catOf))].sort(), [graph, catOf]);
  const colorOf = (cat: string) => CAT_PALETTE[Math.max(0, categories.indexOf(cat)) % CAT_PALETTE.length];

  const [mode, setMode] = useState<"explorer" | "list">("explorer");
  const [tf, setTf] = useState(() => fitView(layout, width, height));
  const [, forceRerender] = useState(0);
  const [hoverNode, setHoverNode] = useState<string | null>(null);
  const [filterOpen, setFilterOpen] = useState(false);
  const [filterQuery, setFilterQuery] = useState("");
  const svgRef = useRef<SVGSVGElement>(null);
  const draggingRef = useRef<string | null>(null);
  const panRef = useRef<{ sx: number; sy: number; ox: number; oy: number } | null>(null);
  const movedRef = useRef(false);

  // Nodes linked to the hovered node (either direction) — the rest get greyed out.
  const neighbors = useMemo(() => {
    const s = new Set<string>();
    if (!hoverNode) return s;
    for (const e of graph.edges) {
      if (e.from === hoverNode) s.add(e.to);
      if (e.to === hoverNode) s.add(e.from);
    }
    return s;
  }, [hoverNode, graph.edges]);

  // Obsidian-style filter: matches by page name (no tags/full-text index in Virtual Worker yet),
  // kept alongside anything directly linked to a match so the topic's connections stay visible.
  const filterKeep = useMemo(() => {
    const q = filterQuery.trim().toLowerCase();
    if (!q) return null;
    const matched = new Set(graph.nodes.filter((n) => n.toLowerCase().includes(q)));
    const all = new Set(matched);
    for (const e of graph.edges) {
      if (matched.has(e.from)) all.add(e.to);
      if (matched.has(e.to)) all.add(e.from);
    }
    return { matched, all };
  }, [filterQuery, graph.nodes, graph.edges]);

  useEffect(() => { setTf(fitView(layout, width, height)); }, [layout]);

  // client px → viewBox coords (viewBox is 900×560 scaled to container width)
  const toVb = (cx: number, cy: number) => {
    const r = svgRef.current!.getBoundingClientRect();
    return { x: (cx - r.left) * (width / r.width), y: (cy - r.top) * (height / r.height) };
  };
  const toWorld = (cx: number, cy: number) => {
    const vb = toVb(cx, cy);
    return { x: (vb.x - tf.x) / tf.k, y: (vb.y - tf.y) / tf.k };
  };

  // Non-passive wheel listener so we can preventDefault and zoom around the cursor.
  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const r = svg.getBoundingClientRect();
      const vbX = (e.clientX - r.left) * (width / r.width), vbY = (e.clientY - r.top) * (height / r.height);
      const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
      setTf((prev) => {
        const k = Math.max(0.2, Math.min(4, prev.k * factor));
        const ratio = k / prev.k;
        return { k, x: vbX - (vbX - prev.x) * ratio, y: vbY - (vbY - prev.y) * ratio };
      });
    };
    svg.addEventListener("wheel", onWheel, { passive: false });
    return () => svg.removeEventListener("wheel", onWheel);
  }, []);

  const zoomBy = (factor: number) => setTf((prev) => {
    const k = Math.max(0.2, Math.min(4, prev.k * factor)), ratio = k / prev.k;
    return { k, x: width / 2 - (width / 2 - prev.x) * ratio, y: height / 2 - (height / 2 - prev.y) * ratio };
  });

  const onMouseMove = (e: React.MouseEvent) => {
    const vb = toVb(e.clientX, e.clientY);
    if (panRef.current) {
      // Snapshot now — panRef is a mutable ref, and a mouseup (endDrag) can null it
      // out before this updater actually runs, since setState updaters can be
      // deferred behind other queued updates from the same burst of events.
      const { ox, oy, sx, sy } = panRef.current;
      setTf((prev) => ({ ...prev, x: ox + vb.x - sx, y: oy + vb.y - sy }));
    } else if (draggingRef.current) {
      const w = toWorld(e.clientX, e.clientY);
      const p = layout.get(draggingRef.current);
      if (p) { movedRef.current = true; p.x = w.x; p.y = w.y; forceRerender((n) => n + 1); }
    }
    // Hover is driven by cursor position, not DOM mouseenter/leave: while panning/zooming,
    // node <g> elements slide under a stationary cursor, and hit-testing that motion fires
    // native enter/leave events with no real pointer movement — a feedback loop that hangs
    // the tab. Nearest-node-within-radius sidesteps it entirely.
    let closest: string | null = null, closestD2 = Infinity;
    for (const n of graph.nodes) {
      const p = layout.get(n)!;
      const dx = p.x * tf.k + tf.x - vb.x, dy = p.y * tf.k + tf.y - vb.y;
      const d2 = dx * dx + dy * dy;
      if (d2 < closestD2) { closestD2 = d2; closest = n; }
    }
    setHoverNode(closestD2 <= 144 ? closest : null);
  };
  const endDrag = () => { draggingRef.current = null; panRef.current = null; };

  return (
    <div className="rounded-2xl border border-pressed bg-canvas p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <div className="flex overflow-hidden rounded-lg border border-pressed">
            <button onClick={() => setMode("explorer")} className={`px-2.5 py-1 text-xs font-medium ${mode === "explorer" ? "bg-primary text-white" : "text-body hover:text-ink"}`}>Explorer</button>
            <button onClick={() => setMode("list")} className={`px-2.5 py-1 text-xs font-medium ${mode === "list" ? "bg-primary text-white" : "text-body hover:text-ink"}`}>List</button>
          </div>
          <span className="text-xs text-mute">{graph.nodes.length} page(s){mode === "explorer" ? " · scroll to zoom, drag to pan/move, click to open" : ""}</span>
        </div>
        {mode === "explorer" && (
          <div className="flex shrink-0 items-center gap-1">
            {filterOpen && (
              <input
                autoFocus
                value={filterQuery}
                onChange={(e) => setFilterQuery(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Escape") { setFilterOpen(false); setFilterQuery(""); } }}
                placeholder="Filter by page name…"
                className="w-28 min-w-0 rounded-lg border border-pressed bg-canvas px-2 py-1 text-xs text-ink outline-none focus:border-primary-line"
              />
            )}
            <button
              onClick={() => { if (filterOpen) { setFilterOpen(false); setFilterQuery(""); } else setFilterOpen(true); }}
              className={`${btnCls} inline-flex items-center justify-center ${filterQuery ? "bg-primary-soft text-primary-deep" : ""}`}
              title="Filter graph by page name"
            >
              <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.65" y2="16.65" />
              </svg>
            </button>
            <button onClick={() => zoomBy(1.2)} className={btnCls} title="Zoom in">+</button>
            <button onClick={() => zoomBy(1 / 1.2)} className={btnCls} title="Zoom out">−</button>
            <button onClick={() => setTf(fitView(layout, width, height))} className={btnCls} title="Fit to view">reset</button>
          </div>
        )}
      </div>
      {mode === "explorer" && categories.length > 1 && (
        <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          {categories.map((c) => (
            <span key={c} className="flex items-center gap-1 text-[11px] text-body">
              <span className="inline-block h-2.5 w-2.5 rounded-full" style={{ background: colorOf(c) }} />{c}
            </span>
          ))}
        </div>
      )}
      {graph.nodes.length === 0 ? (
        <p className="text-xs text-mute">No pages yet.</p>
      ) : mode === "list" ? (
        <div className="space-y-2">
          {graph.nodes.map((n) => {
            const out = graph.edges.filter((e) => e.from === n);
            return (
              <div key={n} className="rounded-xl border border-pressed bg-softer p-2">
                <button onClick={() => onOpenPage(n)} className="text-xs font-semibold text-ink hover:text-primary hover:underline">{n}</button>
                {out.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {out.map((e, i) => (
                      <span key={i} className={`rounded-full border px-2 py-0.5 text-[11px] ${
                        e.resolved ? "border-pressed text-body" : "border-danger text-danger"
                      }`}>→ {e.to}{e.resolved ? "" : " (broken)"}</span>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      ) : (
        <svg
          ref={svgRef}
          viewBox={`0 0 ${width} ${height}`}
          className="w-full rounded-xl border border-pressed bg-softer"
          style={{ height: 560, cursor: panRef.current ? "grabbing" : "grab" }}
          onMouseDown={(e) => { if (e.target === svgRef.current) { const vb = toVb(e.clientX, e.clientY); panRef.current = { sx: vb.x, sy: vb.y, ox: tf.x, oy: tf.y }; } }}
          onMouseMove={onMouseMove}
          onMouseUp={endDrag}
          onMouseLeave={() => { endDrag(); setHoverNode(null); }}
        >
          {/* Positions scale with zoom (translate(tf.x,tf.y) + tf.k) but nodes/labels are
              drawn at a fixed screen size — like Obsidian's graph view, zooming spreads
              the layout out instead of blowing up the circles/text. */}
          {graph.edges.map((e, i) => {
            const a = layout.get(e.from), b = layout.get(e.to);
            if (!a || !b) return null;
            const filterDim = !!filterKeep && !filterKeep.matched.has(e.from) && !filterKeep.matched.has(e.to);
            const hoverDim = !!hoverNode && e.from !== hoverNode && e.to !== hoverNode;
            const dimmed = filterDim || hoverDim;
            return (
              <line
                key={i}
                x1={a.x * tf.k + tf.x} y1={a.y * tf.k + tf.y}
                x2={b.x * tf.k + tf.x} y2={b.y * tf.k + tf.y}
                stroke={e.resolved ? "var(--color-pressed, #ccc)" : "#dc2626"}
                strokeWidth={1}
                strokeDasharray={e.resolved ? undefined : "3,3"}
                style={{ opacity: dimmed ? 0.06 : 1 }}
              />
            );
          })}
          {graph.nodes.map((n) => {
            const p = layout.get(n)!;
            const degree = graph.edges.filter((e) => e.from === n || e.to === n).length;
            // sqrt so a hub with 30 links isn't 30× a leaf's area; base kept small
            const r = 3 + Math.sqrt(degree) * 1.4;
            const filterDim = !!filterKeep && !filterKeep.all.has(n);
            const hoverDim = !!hoverNode && n !== hoverNode && !neighbors.has(n);
            const dimmed = filterDim || hoverDim;
            return (
              <g
                key={n}
                transform={`translate(${p.x * tf.k + tf.x},${p.y * tf.k + tf.y})`}
                className="cursor-pointer"
                style={{ opacity: dimmed ? 0.15 : 1 }}
                onMouseDown={() => { movedRef.current = false; draggingRef.current = n; }}
                onClick={() => { if (!movedRef.current) onOpenPage(n); }}
              >
                <circle r={r} style={{ fill: colorOf(catOf(n)) }} className="opacity-80 hover:opacity-100" />
                {(tf.k > 0.55 || filterKeep?.matched.has(n)) && <text x={r + 4} y={4} className="fill-ink text-[10px] font-medium select-none">{n}</text>}
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}

function TestSearch({ wikiId, onOpenPage }: { wikiId: string; onOpenPage: (path: string) => void }) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (!query.trim()) return;
    setBusy(true);
    try {
      const r = await apiPost<{ results: SearchHit[] }>(`/api/wiki/${wikiId}/search`, { query: query.trim(), k: 5 });
      setHits(r.results);
    } finally { setBusy(false); }
  };

  return (
    <div className="rounded-2xl border border-pressed bg-canvas p-4">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-mute">Ask this wiki</div>
      <div className="flex gap-2">
        <input className={inputCls} value={query} placeholder="Search the wiki pages…"
          onChange={(e) => setQuery(e.target.value)} onKeyDown={(e) => e.key === "Enter" && run()} />
        <button disabled={busy} onClick={run}
          className="shrink-0 rounded-xl bg-primary px-4 py-2 text-sm font-semibold text-white hover:bg-primary-deep disabled:opacity-50">
          {busy ? "…" : "Search"}</button>
      </div>
      {hits && (
        <div className="mt-4 space-y-2">
          {hits.length === 0 && <p className="text-xs text-mute">No pages matched.</p>}
          {hits.map((h) => (
            <button key={h.path} onClick={() => onOpenPage(h.path)}
              className="block w-full rounded-xl border border-pressed bg-softer p-3 text-left hover:border-primary-line">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm font-semibold text-ink">{h.title}</span>
                <span className="shrink-0 text-[11px] text-mute">{h.path}</span>
              </div>
              <p className="mt-1 text-xs text-body">{h.snippet}</p>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
