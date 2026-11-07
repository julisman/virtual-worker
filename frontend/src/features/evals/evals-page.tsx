// Evaluate — benchmark on-device models on a reusable suite of cases, watch the
// run live, read a leaderboard (quality, tool reliability, speed, memory), drill
// into per-case outputs side by side, and promote the winner. Reads /api/eval/*.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch, apiJson, apiPost } from "@/lib/api";
import { useToast } from "@/lib/toast";

// --------------------------------------------------------------------------- //
// Types (mirror routes/eval.py)
// --------------------------------------------------------------------------- //

type Scorer = "auto" | "exact" | "rubric" | "judge" | "tool" | "none";
interface Case {
  prompt: string; system?: string; expected?: string; rubric?: string[] | string; judge?: string;
  tools?: unknown[]; expectedTool?: { name: string; args: Record<string, unknown> }; scorer?: Scorer; taskType?: string;
}
interface Suite { id: string; name: string; description: string; items: Case[]; version: number; createdAt: string; updatedAt: string }
interface LocalModel { backend: string; repo: string; filename?: string; path: string; sizeGb?: number | null; quant?: string | null; textGenerative?: boolean }
interface Provider { id: string; label: string; byok: boolean; hasKey: boolean; models: string[] }
interface Progress { modelIndex: number; modelTotal: number; itemIndex: number; itemTotal: number; currentModel: string | null; phase: string }
interface LeaderRow {
  model: string; cases: number; scoredCases: number; avgScore: number | null; toolReliability: number | null; toolCases: number;
  judgeScore: number | null; judgeCases: number; avgTokensPerSec: number | null; avgLatencyMs: number | null;
  totalTokens: number; peakMemoryBytes: number | null; errors: number; errorRate: number | null; winner: boolean;
}
interface Run {
  id: string; suiteId: string; suiteName: string; suiteVersion: number; status: string;
  models: { path: string; label?: string }[]; options: Record<string, unknown>;
  progress: Progress | null; error: string | null; createdAt: string; startedAt: string | null; finishedAt: string | null;
  leaderboard?: LeaderRow[];
}
interface MatrixCell { score: number | null; output: string; error: string | null; rationale: string | null; latencyMs: number | null; reps: number }
interface Matrix { cases: Case[]; matrix: { itemIndex: number; models: Record<string, MatrixCell> }[] }
interface Template { taskType: string; label: string; item: Case }

const inputCls =
  "w-full rounded-xl border border-pressed bg-canvas px-3 py-2 text-sm text-ink outline-none placeholder:text-mute focus:border-primary focus:ring-2 focus:ring-primary-soft";
const RUNNING = ["pending", "running"];
const SCORER_LABEL: Record<string, string> = { auto: "Auto", exact: "Exact answer", rubric: "Keywords", judge: "LLM judge", tool: "Tool call", none: "Unscored" };

function effectiveScorer(c: Case): string {
  if (c.scorer && c.scorer !== "auto") return c.scorer;
  if (c.expectedTool) return "tool";
  if (c.judge) return "judge";
  if (c.expected) return "exact";
  if (c.rubric && (Array.isArray(c.rubric) ? c.rubric.length : c.rubric)) return "rubric";
  return "none";
}

function modelName(path: string): string {
  const m = path.match(/models--[^/]+--([^/]+)/);
  if (m) return m[1];
  return path.split("/").pop() || path;
}

function fmtBytes(n: number | null): string {
  if (n == null) return "—";
  return n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : `${Math.round(n / 1e6)} MB`;
}

function StatusPill({ status }: { status: string }) {
  const running = RUNNING.includes(status);
  const cls = status === "done" ? "bg-success-soft text-success" : status === "error" ? "bg-danger-soft text-danger"
    : status === "canceled" ? "bg-soft text-body" : running ? "bg-primary-soft text-primary-deep" : "bg-soft text-body";
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 font-mono text-[10px] ${cls}`}>
      {running && <span className="inline-block size-1.5 animate-pulse rounded-full bg-current" />}
      {status}
    </span>
  );
}

function Card({ title, sub, children, right }: { title?: string; sub?: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="rounded-2xl border border-line bg-canvas p-5">
      {title && (
        <div className="mb-4 flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <h3 className="font-display text-sm font-semibold text-ink">{title}</h3>
            {sub && <p className="mt-0.5 text-xs text-body">{sub}</p>}
          </div>
          {right}
        </div>
      )}
      {children}
    </div>
  );
}

function Bar({ value, max }: { value: number | null; max: number }) {
  const pct = value == null || max <= 0 ? 0 : Math.max(2, Math.round((value / max) * 100));
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-soft">
      <div className="h-full rounded-full bg-primary transition-[width] duration-500" style={{ width: `${pct}%` }} />
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Leaderboard
// --------------------------------------------------------------------------- //

function Leaderboard({ rows, onPromote, promoting }: { rows: LeaderRow[]; onPromote: (model: string) => void; promoting: string | null }) {
  if (rows.length === 0) return <p className="py-8 text-center text-sm text-mute">No results yet. Run a benchmark to populate the leaderboard.</p>;
  const maxScore = Math.max(1, ...rows.map((r) => r.avgScore ?? 0));
  const maxTps = Math.max(1, ...rows.map((r) => r.avgTokensPerSec ?? 0));
  const hasTools = rows.some((r) => r.toolCases > 0);
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr className="border-b border-pressed text-left text-[11px] font-semibold uppercase tracking-wide text-mute">
            <th className="py-2 pr-3">Model</th>
            <th className="py-2 pr-3">Quality</th>
            {hasTools && <th className="py-2 pr-3">Tool calls</th>}
            <th className="py-2 pr-3">Tok/s</th>
            <th className="py-2 pr-3">Latency</th>
            <th className="py-2 pr-3">Memory</th>
            <th className="py-2 pr-3">Errors</th>
            <th className="py-2 pr-3"></th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.model} className={`border-b border-line ${r.winner ? "bg-primary-soft/40" : ""}`}>
              <td className="py-3 pr-3">
                <div className="flex items-center gap-2">
                  {r.winner && <span className="rounded-full bg-primary px-2 py-0.5 text-[9px] font-bold uppercase text-white">Winner</span>}
                  <span className="font-medium text-ink" title={r.model}>{modelName(r.model)}</span>
                </div>
              </td>
              <td className="py-3 pr-3" style={{ minWidth: 120 }}>
                <div className="font-display text-lg font-semibold tracking-tight text-ink">{r.avgScore == null ? "—" : `${Math.round(r.avgScore * 100)}%`}</div>
                <Bar value={r.avgScore} max={maxScore} />
                <div className="mt-0.5 text-[10px] text-mute">{r.scoredCases}/{r.cases} scored{r.judgeCases ? ` · judge ${Math.round((r.judgeScore ?? 0) * 100)}%` : ""}</div>
              </td>
              {hasTools && (
                <td className="py-3 pr-3 font-mono text-xs text-body">{r.toolReliability == null ? "—" : `${Math.round(r.toolReliability * 100)}%`}<span className="text-mute"> of {r.toolCases}</span></td>
              )}
              <td className="py-3 pr-3" style={{ minWidth: 100 }}>
                <div className="font-display text-base font-semibold text-ink">{r.avgTokensPerSec?.toFixed(1) ?? "—"}</div>
                <Bar value={r.avgTokensPerSec} max={maxTps} />
              </td>
              <td className="py-3 pr-3 font-mono text-xs text-body">{r.avgLatencyMs == null ? "—" : `${Math.round(r.avgLatencyMs)} ms`}</td>
              <td className="py-3 pr-3 font-mono text-xs text-body">{fmtBytes(r.peakMemoryBytes)}</td>
              <td className="py-3 pr-3 font-mono text-xs"><span className={r.errors > 0 ? "text-danger" : "text-mute"}>{r.errors}/{r.cases}</span></td>
              <td className="py-3 pr-3 text-right">
                <button onClick={() => onPromote(r.model)} disabled={promoting === r.model}
                  className="rounded-lg border border-primary-line bg-primary-soft px-3 py-1.5 text-xs font-bold text-primary-deep hover:bg-primary hover:text-white disabled:opacity-50">
                  {promoting === r.model ? "Promoting…" : "Promote"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Case editor
// --------------------------------------------------------------------------- //

function CaseEditor({ c, i, onChange, onRemove }: { c: Case; i: number; onChange: (p: Partial<Case>) => void; onRemove: () => void }) {
  const kind = effectiveScorer(c);
  const [toolsText, setToolsText] = useState(() => (c.tools ? JSON.stringify(c.tools, null, 1) : ""));
  const [argsText, setArgsText] = useState(() => (c.expectedTool?.args ? JSON.stringify(c.expectedTool.args) : "{}"));
  const [jsonErr, setJsonErr] = useState<string | null>(null);
  const setScorer = (s: Scorer) => onChange({ scorer: s });
  return (
    <div className="rounded-xl border border-pressed bg-softer p-3">
      <div className="flex items-start gap-2">
        <span className="mt-2 w-6 shrink-0 text-right font-mono text-[11px] text-mute">{i + 1}</span>
        <textarea className={`${inputCls} min-h-[42px] min-w-0 flex-1 resize-y`} rows={2} value={c.prompt}
          onChange={(e) => onChange({ prompt: e.target.value })} placeholder="The prompt to send…" />
        <select className={`${inputCls.replace("w-full", "w-44 shrink-0")}`} value={c.scorer ?? "auto"} onChange={(e) => setScorer(e.target.value as Scorer)} title="How to score the answer">
          {(["auto", "exact", "rubric", "judge", "tool", "none"] as Scorer[]).map((s) => <option key={s} value={s}>{SCORER_LABEL[s]}{s === "auto" ? ` (${SCORER_LABEL[kind]})` : ""}</option>)}
        </select>
        <button onClick={onRemove} className="mt-1 rounded-lg px-2 text-mute hover:text-danger" title="Remove">✕</button>
      </div>
      <div className="mt-2 grid grid-cols-1 gap-2 pl-8 sm:grid-cols-2">
        <input className={inputCls} value={c.system ?? ""} onChange={(e) => onChange({ system: e.target.value || undefined })} placeholder="System message (optional)" />
        {(kind === "exact" || kind === "judge" || kind === "none" || kind === "auto") && (
          <input className={inputCls} value={c.expected ?? ""} onChange={(e) => onChange({ expected: e.target.value || undefined })}
            placeholder={kind === "judge" ? "Reference answer for the judge (optional)" : "Expected answer (exact or substring)"} />
        )}
        {kind === "rubric" && (
          <input className={inputCls} value={Array.isArray(c.rubric) ? c.rubric.join(", ") : (c.rubric ?? "")}
            onChange={(e) => onChange({ rubric: e.target.value })} placeholder="Required keywords, comma separated" />
        )}
        {(kind === "judge" || c.judge) && (
          <input className={`${inputCls} sm:col-span-2`} value={c.judge ?? ""} onChange={(e) => onChange({ judge: e.target.value || undefined })}
            placeholder="Grading instructions for the LLM judge" />
        )}
        {kind === "tool" && (
          <>
            <textarea className={`${inputCls} font-mono text-[11px] sm:col-span-2`} rows={4} value={toolsText}
              onChange={(e) => {
                setToolsText(e.target.value);
                try { const v = JSON.parse(e.target.value); onChange({ tools: Array.isArray(v) ? v : [v] }); setJsonErr(null); }
                catch { setJsonErr("tools: not valid JSON"); }
              }} placeholder='Tool schemas (OpenAI format): [{"type":"function","function":{"name":"get_weather","parameters":{…}}}]' />
            <input className={inputCls} value={c.expectedTool?.name ?? ""}
              onChange={(e) => onChange({ expectedTool: { name: e.target.value, args: c.expectedTool?.args ?? {} } })} placeholder="Expected tool name" />
            <input className={`${inputCls} font-mono text-[11px]`} value={argsText}
              onChange={(e) => {
                setArgsText(e.target.value);
                try { const v = JSON.parse(e.target.value || "{}"); onChange({ expectedTool: { name: c.expectedTool?.name ?? "", args: v } }); setJsonErr(null); }
                catch { setJsonErr("expected args: not valid JSON"); }
              }} placeholder='Expected arguments (subset), e.g. {"city": "Paris"}' />
          </>
        )}
      </div>
      {jsonErr && <p className="mt-1 pl-8 text-[11px] text-danger">{jsonErr}</p>}
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Case drill-down
// --------------------------------------------------------------------------- //

function CaseMatrix({ runId }: { runId: string }) {
  const [data, setData] = useState<Matrix | null>(null);
  const [open, setOpen] = useState<number | null>(null);
  useEffect(() => { apiJson<Matrix>(`/api/eval/runs/${runId}/matrix`).then(setData).catch(() => setData(null)); }, [runId]);
  if (!data || data.matrix.length === 0) return null;
  const models = Array.from(new Set(data.matrix.flatMap((m) => Object.keys(m.models))));
  return (
    <div className="mt-4 border-t border-line pt-4">
      <h4 className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-mute">Per case</h4>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead><tr className="text-left text-[10px] uppercase text-mute"><th className="py-1 pr-2">#</th><th className="py-1 pr-2">Prompt</th>{models.map((m) => <th key={m} className="py-1 pr-2">{modelName(m)}</th>)}</tr></thead>
          <tbody>
            {data.matrix.map((row) => (
              <>
                <tr key={row.itemIndex} className="cursor-pointer border-t border-line hover:bg-soft" onClick={() => setOpen(open === row.itemIndex ? null : row.itemIndex)}>
                  <td className="py-1.5 pr-2 font-mono text-mute">{row.itemIndex + 1}</td>
                  <td className="max-w-[260px] truncate py-1.5 pr-2 text-body">{data.cases[row.itemIndex]?.prompt ?? ""}</td>
                  {models.map((m) => {
                    const cell = row.models[m];
                    const s = cell?.score;
                    return <td key={m} className={`py-1.5 pr-2 font-mono ${cell?.error ? "text-danger" : s == null ? "text-mute" : s >= 0.99 ? "text-success" : s <= 0.01 ? "text-danger" : "text-warn"}`}>
                      {cell ? (cell.error ? "error" : s == null ? "—" : `${Math.round(s * 100)}%`) : ""}</td>;
                  })}
                </tr>
                {open === row.itemIndex && (
                  <tr key={`${row.itemIndex}-d`} className="border-t border-line bg-softer">
                    <td colSpan={2 + models.length} className="p-3">
                      <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${Math.min(models.length, 3)}, minmax(0, 1fr))` }}>
                        {models.map((m) => {
                          const cell = row.models[m];
                          return (
                            <div key={m} className="rounded-xl border border-line bg-canvas p-2">
                              <div className="mb-1 flex items-center justify-between text-[10px] text-mute"><span className="font-medium text-ink">{modelName(m)}</span><span>{cell?.latencyMs != null ? `${Math.round(cell.latencyMs)} ms` : ""}</span></div>
                              <pre className="max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11px] text-body">{cell?.error || cell?.output || "(empty)"}</pre>
                              {cell?.rationale && <p className="mt-1 text-[10px] text-mute">{cell.rationale}</p>}
                            </div>
                          );
                        })}
                      </div>
                    </td>
                  </tr>
                )}
              </>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------- //
// Page
// --------------------------------------------------------------------------- //

export function EvalsPage() {
  const [suites, setSuites] = useState<Suite[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editor, setEditor] = useState<Suite | null>(null);
  const [templates, setTemplates] = useState<Template[]>([]);
  const [models, setModels] = useState<LocalModel[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [temperature, setTemperature] = useState(0);
  const [maxTokens, setMaxTokens] = useState(512);
  const [repetitions, setRepetitions] = useState(1);
  const [judgeMode, setJudgeMode] = useState<"none" | "self" | "provider">("none");
  const [judgeProvider, setJudgeProvider] = useState("");
  const [judgeModel, setJudgeModel] = useState("");
  const [runs, setRuns] = useState<Run[]>([]);
  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [board, setBoard] = useState<LeaderRow[]>([]);
  const [promoting, setPromoting] = useState<string | null>(null);
  const toast = useToast();
  const esRef = useRef<EventSource | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const loadSuites = useCallback(async () => {
    try {
      const list = (await apiJson<{ suites: Suite[] }>("/api/eval/suites")).suites;
      setSuites(list);
      setSelectedId((cur) => (cur && list.some((s) => s.id === cur) ? cur : list[0]?.id ?? null));
    } catch { /* */ }
  }, []);
  const loadModels = useCallback(async () => {
    try {
      const list = (await apiJson<{ models: LocalModel[] }>("/api/hub/local")).models;
      setModels(list.filter((m) => m.textGenerative !== false));
    } catch { /* */ }
  }, []);
  const loadRuns = useCallback(async () => {
    try { setRuns((await apiJson<{ runs: Run[] }>("/api/eval/runs")).runs); } catch { /* */ }
  }, []);

  useEffect(() => {
    loadSuites(); loadModels(); loadRuns();
    apiJson<{ templates: Template[] }>("/api/eval/templates").then((r) => setTemplates(r.templates)).catch(() => {});
    apiJson<{ providers: Provider[] }>("/api/providers").then((r) => setProviders(r.providers.filter((p) => p.hasKey))).catch(() => {});
  }, [loadSuites, loadModels, loadRuns]);
  useEffect(() => { setEditor(suites.find((s) => s.id === selectedId) ?? null); }, [suites, selectedId]);
  useEffect(() => () => esRef.current?.close(), []);

  const newSuite = async () => {
    const s = await apiPost<Suite>("/api/eval/suites", { name: "New suite", description: "", items: [{ prompt: "What is the capital of France?", expected: "Paris" }] });
    await loadSuites(); setSelectedId(s.id);
  };
  const fromFeedback = async () => {
    try {
      const s = await apiPost<Suite>("/api/eval/suites/from-feedback", {});
      await loadSuites(); setSelectedId(s.id);
      toast.success(`Built ${s.items.length} judged cases from your feedback`);
    } catch (e) { toast.error(e instanceof Error ? e.message : String(e)); }
  };
  const importSuite = async (file: File) => {
    try {
      const text = await file.text();
      const res = await apiFetch("/api/eval/suites/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: text });
      const j = await res.json();
      if (!res.ok) throw new Error(j?.detail || `import failed (${res.status})`);
      await loadSuites(); setSelectedId(j.id); toast.success("Suite imported");
    } catch (e) { toast.error(e instanceof Error ? e.message : String(e)); }
    finally { if (fileRef.current) fileRef.current.value = ""; }
  };
  const saveSuite = async () => {
    if (!editor) return;
    try {
      await apiJson(`/api/eval/suites/${editor.id}`, { method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: editor.name, description: editor.description, items: editor.items }) });
      await loadSuites(); toast.success("Suite saved");
    } catch (e) { toast.error(`Save failed: ${e instanceof Error ? e.message : String(e)}`); }
  };
  const deleteSuite = async () => {
    if (!editor || !window.confirm(`Delete suite "${editor.name}"?`)) return;
    await apiJson(`/api/eval/suites/${editor.id}`, { method: "DELETE" }).catch(() => {});
    await loadSuites();
  };
  const patchEditor = (p: Partial<Suite>) => setEditor((e) => (e ? { ...e, ...p } : e));
  const patchCase = (i: number, p: Partial<Case>) => setEditor((e) => (e ? { ...e, items: e.items.map((c, j) => (j === i ? { ...c, ...p } : c)) } : e));
  const addCase = (tpl?: Case) => setEditor((e) => (e ? { ...e, items: [...e.items, tpl ? { ...tpl } : { prompt: "" }] } : e));
  const removeCase = (i: number) => setEditor((e) => (e ? { ...e, items: e.items.filter((_, j) => j !== i) } : e));
  const togglePick = (path: string) => setPicked((s) => { const n = new Set(s); if (n.has(path)) n.delete(path); else n.add(path); return n; });

  const refreshBoard = useCallback(async (runId: string) => {
    try { setBoard((await apiJson<{ leaderboard: LeaderRow[] }>(`/api/eval/runs/${runId}/leaderboard`)).leaderboard); } catch { /* */ }
  }, []);
  const attachStream = useCallback((run: Run) => {
    esRef.current?.close();
    const es = new EventSource(`/api/eval/runs/${run.id}/stream`);
    esRef.current = es;
    es.onmessage = (e) => {
      let evt: Record<string, unknown>;
      try { evt = JSON.parse(e.data); } catch { return; }
      if ((evt.type === "snapshot" || evt.type === "done") && Array.isArray(evt.leaderboard)) setBoard(evt.leaderboard as LeaderRow[]);
      if (evt.type === "progress" && evt.progress) setActiveRun((r) => (r ? { ...r, progress: evt.progress as Progress } : r));
      if (evt.type === "result" || evt.type === "model_failed") refreshBoard(run.id);
      if (evt.type === "done") { setActiveRun((r) => (r ? { ...r, status: (evt.status as string) || "done" } : r)); es.close(); loadRuns(); }
    };
  }, [refreshBoard, loadRuns]);

  const startRun = async () => {
    if (!editor || picked.size === 0) return;
    const judge = judgeMode === "none" ? undefined : { mode: judgeMode, provider: judgeProvider, model: judgeModel };
    const run = await apiPost<Run>("/api/eval/runs", {
      suiteId: editor.id, models: [...picked].map((path) => ({ path, label: modelName(path) })),
      options: { temperature, maxTokens, repetitions, judge },
    }).catch((e) => { toast.error(String(e.message || e)); return null; });
    if (!run) return;
    setActiveRun(run); setBoard([]); attachStream(run); loadRuns();
  };
  const openRun = async (runId: string) => {
    const run = await apiJson<Run>(`/api/eval/runs/${runId}`);
    setActiveRun(run); setBoard(run.leaderboard ?? []);
    if (RUNNING.includes(run.status)) attachStream(run); else esRef.current?.close();
  };
  const cancelRun = async () => { if (activeRun) await apiPost(`/api/eval/runs/${activeRun.id}/cancel`, {}).catch(() => {}); };
  const deleteRun = async () => {
    if (!activeRun || !window.confirm("Delete this run and its results?")) return;
    await apiJson(`/api/eval/runs/${activeRun.id}`, { method: "DELETE" }).catch((e) => toast.error(String(e.message || e)));
    setActiveRun(null); setBoard([]); loadRuns();
  };
  const promote = async (model: string) => {
    if (!activeRun) return;
    setPromoting(model);
    try { await apiPost(`/api/eval/runs/${activeRun.id}/promote`, { model }); toast.success(`Promoted ${modelName(model)}; it is now the active model.`); }
    catch (e) { toast.error(String((e as Error).message)); }
    finally { setPromoting(null); }
  };

  const isRunning = activeRun != null && RUNNING.includes(activeRun.status);
  const prog = activeRun?.progress;
  const judgeCases = editor?.items.filter((c) => effectiveScorer(c) === "judge").length ?? 0;

  return (
    <div className="mx-auto flex h-full max-w-6xl flex-col gap-5 overflow-y-auto p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-display text-2xl font-semibold tracking-tight text-ink">Evaluate</h1>
          <p className="mt-0.5 text-sm text-body">Benchmark your on-device models on a suite of cases: quality, tool calls, speed, memory, and a winner you can promote.</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={fromFeedback} className="rounded-xl border border-pressed px-3 py-2 text-sm font-medium text-body hover:bg-soft" title="Up-rated and corrected chat replies become judged cases">From feedback</button>
          <button onClick={() => fileRef.current?.click()} className="rounded-xl border border-pressed px-3 py-2 text-sm font-medium text-body hover:bg-soft">Import</button>
          <input ref={fileRef} type="file" accept=".json" className="hidden" onChange={(e) => { const f = e.target.files?.[0]; if (f) importSuite(f); }} />
          <button onClick={newSuite} className="rounded-xl bg-primary px-4 py-2 text-sm font-bold text-white hover:bg-primary-deep">New suite</button>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[260px_1fr]">
        <Card title="Suites">
          <div className="flex flex-col gap-1">
            {suites.length === 0 && <p className="text-xs text-mute">No suites yet. Create one, import one, or build one from feedback.</p>}
            {suites.map((s) => (
              <button key={s.id} onClick={() => setSelectedId(s.id)}
                className={`truncate rounded-lg px-3 py-2 text-left text-sm ${s.id === selectedId ? "bg-primary-soft font-bold text-primary-deep" : "text-body hover:bg-soft"}`}>
                {s.name}<span className="ml-1 text-[11px] text-mute">· {s.items.length} · v{s.version}</span>
              </button>
            ))}
          </div>
        </Card>

        <div className="flex flex-col gap-5">
          {editor ? (
            <Card>
              <div className="mb-3 flex items-center gap-2">
                <input className={inputCls} value={editor.name} onChange={(e) => patchEditor({ name: e.target.value })} placeholder="Suite name" />
                <button onClick={saveSuite} className="shrink-0 rounded-xl bg-primary px-4 py-2 text-sm font-bold text-white hover:bg-primary-deep">Save</button>
                <a href={`/api/eval/suites/${editor.id}/export`} download className="shrink-0 rounded-xl border border-pressed px-3 py-2 text-sm font-medium text-body hover:bg-soft">Export</a>
                <button onClick={deleteSuite} className="shrink-0 rounded-xl border border-pressed px-3 py-2 text-sm font-medium text-body hover:bg-soft hover:text-danger">Delete</button>
              </div>
              <input className={`${inputCls} mb-4`} value={editor.description} onChange={(e) => patchEditor({ description: e.target.value })} placeholder="Description (optional)" />
              <div className="flex flex-col gap-2">
                {editor.items.map((c, i) => (
                  <CaseEditor key={i} c={c} i={i} onChange={(p) => patchCase(i, p)} onRemove={() => removeCase(i)} />
                ))}
                <div className="flex flex-wrap items-center gap-2">
                  <button onClick={() => addCase()} className="rounded-lg border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft">+ Add case</button>
                  {templates.map((t) => (
                    <button key={t.taskType} onClick={() => addCase(t.item)} className="rounded-lg border border-pressed px-3 py-1.5 text-xs text-body hover:bg-soft" title="Add a starter case of this type">+ {t.label}</button>
                  ))}
                </div>
                <p className="text-[11px] text-mute">
                  Exact answers score by normalized match or substring; keywords by the fraction present; tool cases by the tool name and
                  expected arguments; judged cases by an LLM grader chosen at run time. Changing the cases bumps the suite version, recorded on every run.
                </p>
              </div>
            </Card>
          ) : (
            <Card><p className="text-sm text-mute">Create or select a suite to edit its cases.</p></Card>
          )}

          {editor && (
            <Card title="Compare models" sub="Each model is activated in turn through the managed runtime (one at a time), then run on every case.">
              <div className="mb-4 grid grid-cols-1 gap-2 sm:grid-cols-2">
                {models.length === 0 && <p className="text-xs text-mute">No on-device models found. Download some under Models.</p>}
                {models.map((m) => (
                  <label key={m.path} className={`flex cursor-pointer items-center gap-2 rounded-xl border px-3 py-2 text-sm ${picked.has(m.path) ? "border-primary-line bg-primary-soft" : "border-pressed bg-softer hover:bg-soft"}`}>
                    <input type="checkbox" checked={picked.has(m.path)} onChange={() => togglePick(m.path)} className="accent-[var(--color-primary)]" />
                    <span className="min-w-0 flex-1 truncate font-medium text-ink" title={m.path}>{modelName(m.path)}</span>
                    <span className="shrink-0 font-mono text-[10px] text-mute">{m.quant ?? m.backend}{m.sizeGb ? ` · ${m.sizeGb}GB` : ""}</span>
                  </label>
                ))}
              </div>
              <div className="flex flex-wrap items-end gap-4">
                <label className="text-xs font-medium text-body">Temperature
                  <input type="number" step="0.1" min="0" max="2" value={temperature} onChange={(e) => setTemperature(Number(e.target.value))} className={`${inputCls} mt-1 w-24`} />
                </label>
                <label className="text-xs font-medium text-body">Max tokens
                  <input type="number" min="1" value={maxTokens} onChange={(e) => setMaxTokens(Number(e.target.value))} className={`${inputCls} mt-1 w-28`} />
                </label>
                <label className="text-xs font-medium text-body">Repetitions
                  <input type="number" min="1" max="5" value={repetitions} onChange={(e) => setRepetitions(Number(e.target.value))} className={`${inputCls} mt-1 w-24`} />
                </label>
                <label className="text-xs font-medium text-body">LLM judge{judgeCases ? ` (${judgeCases} judged cases)` : ""}
                  <select className={`${inputCls} mt-1 w-56`} value={judgeMode} onChange={(e) => setJudgeMode(e.target.value as "none" | "self" | "provider")}>
                    <option value="none">None (judged cases stay unscored)</option>
                    <option value="self">The model under test</option>
                    {providers.length > 0 && <option value="provider">An external provider</option>}
                  </select>
                </label>
                {judgeMode === "provider" && (
                  <>
                    <label className="text-xs font-medium text-body">Provider
                      <select className={`${inputCls} mt-1 w-40`} value={judgeProvider} onChange={(e) => { setJudgeProvider(e.target.value); setJudgeModel(""); }}>
                        <option value="">pick…</option>
                        {providers.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                      </select>
                    </label>
                    <label className="text-xs font-medium text-body">Judge model
                      <input list="judge-models" className={`${inputCls} mt-1 w-48 font-mono text-xs`} value={judgeModel} onChange={(e) => setJudgeModel(e.target.value)} placeholder="provider default" />
                      <datalist id="judge-models">{providers.find((p) => p.id === judgeProvider)?.models.map((m) => <option key={m} value={m} />)}</datalist>
                    </label>
                  </>
                )}
                <button onClick={startRun} disabled={picked.size === 0 || isRunning || (judgeMode === "provider" && !judgeProvider)}
                  className="ml-auto rounded-xl bg-primary px-5 py-2.5 text-sm font-bold text-white hover:bg-primary-deep disabled:opacity-50">
                  {isRunning ? "Running…" : `Run benchmark (${picked.size})`}
                </button>
              </div>
              <p className="mt-2 text-[11px] text-mute">A run switches the active model. When it finishes, the runtime is restored to whatever was active before; use Promote to keep a winner.</p>
            </Card>
          )}

          {activeRun && (
            <Card>
              <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-3">
                  <h3 className="font-display text-sm font-semibold text-ink">{activeRun.suiteName} <span className="text-mute">v{activeRun.suiteVersion}</span></h3>
                  <StatusPill status={activeRun.status} />
                  {Number(activeRun.options?.repetitions ?? 1) > 1 && <span className="text-[11px] text-mute">× {String(activeRun.options.repetitions)} reps</span>}
                </div>
                <div className="flex items-center gap-2">
                  {!isRunning && <a href={`/api/eval/runs/${activeRun.id}/export?format=csv`} download className="rounded-lg border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft">CSV</a>}
                  {!isRunning && <a href={`/api/eval/runs/${activeRun.id}/export`} download className="rounded-lg border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft">JSON</a>}
                  {isRunning
                    ? <button onClick={cancelRun} className="rounded-lg border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft hover:text-danger">Cancel</button>
                    : <button onClick={deleteRun} className="rounded-lg border border-pressed px-3 py-1.5 text-xs font-medium text-body hover:bg-soft hover:text-danger">Delete</button>}
                </div>
              </div>
              {isRunning && prog && (
                <div className="mb-4">
                  <div className="flex justify-between text-xs text-body">
                    <span>{prog.phase === "activating" ? `Activating ${prog.currentModel}…` : `Model ${prog.modelIndex + 1}/${prog.modelTotal} · case ${prog.itemIndex + 1}/${prog.itemTotal}`}</span>
                    <span className="font-mono text-mute">{prog.currentModel}</span>
                  </div>
                  <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-soft">
                    <div className="h-full rounded-full bg-primary transition-[width] duration-300"
                      style={{ width: `${prog.modelTotal && prog.itemTotal ? Math.round(((prog.modelIndex * prog.itemTotal + prog.itemIndex) / (prog.modelTotal * prog.itemTotal)) * 100) : 0}%` }} />
                  </div>
                </div>
              )}
              {activeRun.error && <p className="mb-3 rounded-lg bg-danger-soft px-3 py-2 text-xs text-danger">{activeRun.error}</p>}
              <Leaderboard rows={board} onPromote={promote} promoting={promoting} />
              {!isRunning && board.length > 0 && <CaseMatrix runId={activeRun.id} />}
            </Card>
          )}

          {runs.length > 0 && (
            <Card title="Recent runs">
              <div className="flex flex-col gap-1">
                {runs.slice(0, 12).map((r) => (
                  <button key={r.id} onClick={() => openRun(r.id)}
                    className={`flex items-center justify-between rounded-lg px-3 py-2 text-left text-sm hover:bg-soft ${activeRun?.id === r.id ? "bg-soft" : ""}`}>
                    <span className="truncate text-body">{r.suiteName} <span className="text-mute">v{r.suiteVersion} · {r.models.length} models</span></span>
                    <span className="flex items-center gap-2"><span className="font-mono text-[10px] text-mute">{new Date(r.createdAt).toLocaleString()}</span><StatusPill status={r.status} /></span>
                  </button>
                ))}
              </div>
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}

export default EvalsPage;
