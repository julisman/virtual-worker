// useJobStream — the shared EventSource lifecycle for a linked-repository job.
// Opens /api/wiki/{wikiId}/jobs/{jobId}/stream, appends log lines, tracks the
// terminal status + exit code, keeps a live elapsed ticker while running, and
// tears the stream down on unmount. Extracted verbatim from the duplicated
// logic in OperationRow and DiscoveryCard — no behavior change.

import { useCallback, useEffect, useRef, useState } from "react";
import { apiPost } from "@/lib/api";
import type { JobStatus } from "./wiki-shared";

export function useJobStream(wikiId: string): {
  status: JobStatus | "idle";
  lines: string[];
  exitCode: number | null;
  elapsed: number;
  start: (jobId: string) => void;
  cancel: () => Promise<void>;
  reset: () => void;
} {
  const [status, setStatus] = useState<JobStatus | "idle">("idle");
  const [lines, setLines] = useState<string[]>([]);
  const [exitCode, setExitCode] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const esRef = useRef<EventSource | null>(null);
  const jobIdRef = useRef<string | null>(null);
  const startedRef = useRef<number>(0);

  // Tear down the SSE stream if the owner unmounts (e.g. switching wiki) mid-run.
  useEffect(() => () => esRef.current?.close(), []);

  // Live duration while running.
  useEffect(() => {
    if (status !== "running") return;
    const t = setInterval(() => setElapsed(Math.round((Date.now() - startedRef.current) / 1000)), 250);
    return () => clearInterval(t);
  }, [status]);

  const finish = useCallback((s: JobStatus, code: number | null) => {
    esRef.current?.close(); esRef.current = null;
    setElapsed(Math.round((Date.now() - startedRef.current) / 1000));
    setExitCode(code); setStatus(s);
  }, []);

  const start = useCallback((jobId: string) => {
    jobIdRef.current = jobId;
    setLines([]); setExitCode(null); setElapsed(0);
    startedRef.current = Date.now(); setStatus("running");
    const es = new EventSource(`/api/wiki/${wikiId}/jobs/${jobId}/stream`);
    esRef.current = es;
    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data) as { line?: string; status?: JobStatus; exitCode?: number | null };
        if (typeof d.line === "string") setLines((prev) => [...prev, d.line as string]);
        else if (d.status) finish(d.status, d.exitCode ?? null);
      } catch { /* ignore malformed frame */ }
    };
    es.onerror = () => { setStatus((cur) => (cur === "running" ? "failed" : cur)); esRef.current?.close(); esRef.current = null; };
  }, [wikiId, finish]);

  const cancel = useCallback(async () => {
    const jobId = jobIdRef.current;
    if (!jobId) return;
    try { await apiPost(`/api/wiki/${wikiId}/jobs/${jobId}/cancel`, {}); } catch { /* best effort */ }
    finish("cancelled", null);
  }, [wikiId, finish]);

  const reset = useCallback(() => {
    esRef.current?.close(); esRef.current = null;
    jobIdRef.current = null; startedRef.current = 0;
    setLines([]); setExitCode(null); setElapsed(0); setStatus("idle");
  }, []);

  return { status, lines, exitCode, elapsed, start, cancel, reset };
}
