"""Benchmark orchestrator for evaluation suites (PRD §8.14).

Runs a suite across several on-device models, one model at a time. For each
selected model the runner:

  1. **Activates** it through the managed runtime (``llama_manager.start``,
     which stops whatever was running; only one model fits in memory) and waits
     until the runtime reports healthy.
  2. **Fires every case** (``repetitions`` times) through the local gateway
     (``gateway.upstream.chat_completion``), the same path normal inference
     uses, so ``ApiDebugMiddleware`` captures every call. Tool cases send the
     case's tool schemas so the model can emit tool calls.
  3. **Scores** the output: exact / substring match, keyword rubric, tool-call
     check, or an LLM judge (the model under test, or a configured provider).
  4. Samples the model server's resident memory and records one result row
     with output, score, rationale, latency, tokens and memory.

Aggregation into the per-model leaderboard lives in ``eval_store``.

**Activation safety.** A run necessarily switches the active model. Before the
run the active model is snapshotted; when the run ends (done / canceled /
error) it is restored, or the runtime is stopped if nothing was active before.
"Promote" is the explicit way to keep an evaluated model active.

``_snapshot_active``, ``_activate``, ``_wait_healthy``, ``_active_model_id``,
``_chat_completion``, ``_judge_completion``, ``_rss_bytes`` and ``_restore``
are module-level seams so tests can run the whole engine without a model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from storage import eval_store

logger = logging.getLogger(__name__)

_TASKS: dict[str, asyncio.Task] = {}
_CANCEL: set[str] = set()
_SUBS: dict[str, set[asyncio.Queue]] = {}
_ACTIVATE_TIMEOUT_S = 600


# --------------------------------------------------------------------------- #
# Scoring (pure functions)
# --------------------------------------------------------------------------- #

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def _normalize(s: str) -> str:
    return _WS.sub(" ", _PUNCT.sub(" ", (s or "").lower())).strip()


def score_exact(output: str, expected: str) -> float:
    out_n, exp_n = _normalize(output), _normalize(expected)
    return 1.0 if (exp_n and (out_n == exp_n or exp_n in out_n)) else 0.0


def score_rubric(output: str, keywords: list[str]) -> float | None:
    keywords = [k for k in keywords if str(k).strip()]
    if not keywords:
        return None
    out_n = _normalize(output)
    return round(sum(1 for k in keywords if _normalize(str(k)) in out_n) / len(keywords), 4)


def _loose_equal(a, b) -> bool:
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a == b
    return _normalize(str(a)) == _normalize(str(b))


def score_tool(tool_calls: list[dict], expected: dict) -> tuple[float, str]:
    """1.0 when some call matches ``expected.name`` and every ``expected.args``
    entry is present with an equal (loosely compared) value; else 0.0."""
    want_name = str(expected.get("name") or "")
    want_args = expected.get("args") or {}
    if not tool_calls:
        return 0.0, "no tool call was made"
    names = []
    for c in tool_calls:
        name = c.get("name") or ""
        names.append(name)
        if name != want_name:
            continue
        args = c.get("args") or {}
        missing = [k for k in want_args if k not in args]
        wrong = [k for k in want_args if k in args and not _loose_equal(args[k], want_args[k])]
        if not missing and not wrong:
            return 1.0, f"called {name} with the expected arguments"
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if wrong:
            parts.append("wrong " + ", ".join(f"{k}={args[k]!r}" for k in wrong))
        return 0.0, f"called {name} but " + "; ".join(parts)
    return 0.0, f"called {', '.join(names)} instead of {want_name}"


def score_item(output: str, item: dict, *, tool_calls: list[dict] | None = None) -> tuple[float | None, bool]:
    """Deterministic scorers only; ``(score, scored)``. Judge cases return
    ``(None, False)`` here and are scored by :func:`judge_item`."""
    kind = eval_store.effective_scorer(item)
    if kind == "tool":
        s, _ = score_tool(tool_calls or [], item.get("expectedTool") or {})
        return s, True
    if kind == "exact" and item.get("expected"):
        return score_exact(output, item["expected"]), True
    if kind == "rubric" and item.get("rubric"):
        s = score_rubric(output, item["rubric"])
        return (s, True) if s is not None else (None, False)
    return None, False


_JUDGE_SYSTEM = (
    "You are a strict grader. Read the task, the reference (if any), the grading "
    "instructions and the candidate answer. Reply with a single JSON object: "
    '{"score": <number from 0 to 1>, "rationale": "<one or two sentences>"} and nothing else.'
)


def judge_prompt(item: dict, output: str) -> list[dict]:
    parts = [f"Task given to the model:\n{item.get('prompt', '')}"]
    if item.get("system"):
        parts.append(f"System instructions the model had:\n{item['system']}")
    if item.get("expected"):
        parts.append(f"Reference answer:\n{item['expected']}")
    parts.append(f"Grading instructions:\n{item.get('judge') or 'Score correctness and completeness.'}")
    parts.append(f"Candidate answer:\n{output or '(empty)'}")
    return [{"role": "system", "content": _JUDGE_SYSTEM}, {"role": "user", "content": "\n\n".join(parts)}]


def parse_judge(text: str) -> tuple[float | None, str]:
    """Pull ``score`` and ``rationale`` from the judge's reply, tolerating prose
    around the JSON and bare numbers."""
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            sc = float(d.get("score"))
            if sc > 1.0:
                sc = sc / 10.0 if sc <= 10 else sc / 100.0
            return max(0.0, min(1.0, round(sc, 4))), str(d.get("rationale") or "")[:500]
        except (ValueError, TypeError, AttributeError):
            pass
    m = re.search(r"(?<![\d.])(0(?:\.\d+)?|1(?:\.0+)?)(?![\d.])", text)
    if m:
        return max(0.0, min(1.0, round(float(m.group(1)), 4))), text[:300]
    return None, "judge reply could not be parsed: " + text[:200]


# --------------------------------------------------------------------------- #
# Runtime / gateway seams (monkeypatched in tests)
# --------------------------------------------------------------------------- #

async def _snapshot_active() -> dict | None:
    from runtime import llama_manager
    st = await llama_manager.status()
    if st.get("running") and st.get("modelPath"):
        return {"path": st["modelPath"], "ctx": st.get("ctx")}
    return None


async def _activate(path: str, ctx: int | None = None) -> dict:
    from runtime import llama_manager
    return await llama_manager.start(path, ctx=ctx, force=True)


async def _wait_healthy(timeout: float = _ACTIVATE_TIMEOUT_S) -> bool:
    from runtime import llama_manager
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = await llama_manager.status()
        if st.get("healthy"):
            return True
        if st.get("failed"):
            return False
        await asyncio.sleep(1.0)
    return False


async def _active_model_id() -> str | None:
    from runtime import llama_manager
    return await llama_manager._active_model_id()


async def _chat_completion(payload: dict) -> dict:
    from gateway import upstream
    return await upstream.chat_completion(payload)


async def _judge_completion(messages: list[dict], judge: dict, local_model_id: str | None) -> str:
    """Ask the judge: the model under test (``mode: self``) or a configured
    provider (``mode: provider``) through the normal BYOK egress."""
    if judge.get("mode") == "provider" and judge.get("provider"):
        from gateway import egress, providers
        pseudo = {"modelSource": "byok", "provider": judge["provider"], "model": judge.get("model") or None}
        model, target = await providers.resolve_target(pseudo)
        parts: list[str] = []
        async for chunk in egress.stream({"model": model, "messages": messages, "temperature": 0, "stream": True}, target):
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                if delta.get("content"):
                    parts.append(delta["content"])
        return "".join(parts)
    resp = await _chat_completion({"model": local_model_id, "messages": messages, "temperature": 0,
                                   "max_tokens": 300, "stream": False})
    text, _, _ = _parse_completion(resp)
    return text


def _rss_bytes() -> int | None:
    """Resident memory of the model server right now."""
    try:
        from routes.monitor import _process_rss_bytes
        from runtime import llama_manager
        pid = llama_manager._state.get("pid") if hasattr(llama_manager, "_state") else None
        return _process_rss_bytes(pid)
    except Exception:  # noqa: BLE001
        return None


async def _restore(snapshot: dict | None) -> None:
    from runtime import llama_manager
    try:
        if snapshot and snapshot.get("path"):
            await llama_manager.start(snapshot["path"], ctx=snapshot.get("ctx"), force=True)
        else:
            llama_manager.stop()
    except Exception:  # pragma: no cover
        logger.warning("eval: failed to restore runtime after run", exc_info=True)


# --------------------------------------------------------------------------- #
# Pub/sub for SSE progress
# --------------------------------------------------------------------------- #

def subscribe(run_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _SUBS.setdefault(run_id, set()).add(q)
    return q


def unsubscribe(run_id: str, q: asyncio.Queue) -> None:
    _SUBS.get(run_id, set()).discard(q)


def _emit(run_id: str, evt: dict) -> None:
    for q in list(_SUBS.get(run_id, set())):
        try:
            q.put_nowait(evt)
        except Exception:  # noqa: BLE001
            pass


def _publish_progress(run_id: str, progress: dict) -> None:
    eval_store.set_progress(run_id, progress)
    _emit(run_id, {"type": "progress", "progress": progress})


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _build_messages(item: dict) -> list[dict]:
    msgs: list[dict] = []
    if item.get("system"):
        msgs.append({"role": "system", "content": str(item["system"])})
    msgs.append({"role": "user", "content": str(item.get("prompt") or "")})
    return msgs


def _parse_completion(resp: dict) -> tuple[str, int | None, int | None]:
    text = ""
    try:
        text = resp["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError, AttributeError):
        text = ""
    usage = resp.get("usage") or {}
    return text, usage.get("prompt_tokens"), usage.get("completion_tokens")


def _parse_tool_calls(resp: dict) -> list[dict]:
    out: list[dict] = []
    try:
        calls = resp["choices"][0]["message"].get("tool_calls") or []
    except (KeyError, IndexError, TypeError, AttributeError):
        return out
    for c in calls:
        fn = (c or {}).get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except ValueError:
                args = {"_raw": args}
        out.append({"name": fn.get("name") or "", "args": args if isinstance(args, dict) else {}})
    return out


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def start_run(run_id: str) -> None:
    task = asyncio.create_task(run_eval(run_id))
    _TASKS[run_id] = task
    task.add_done_callback(lambda _t: _TASKS.pop(run_id, None))


def cancel_run(run_id: str) -> bool:
    run = eval_store.get_run(run_id)
    if not run or run["status"] not in ("pending", "running"):
        return False
    _CANCEL.add(run_id)
    return True


def active_runs() -> int:
    return sum(1 for t in _TASKS.values() if not t.done())


async def _run_case(run_id: str, label: str, model_id: str | None, item: dict, i: int, rep: int,
                    options: dict) -> None:
    payload: dict = {
        "model": model_id, "messages": _build_messages(item),
        "temperature": options.get("temperature", 0.0), "max_tokens": int(options.get("maxTokens", 512)),
        "stream": False,
    }
    if options.get("seed") is not None:
        payload["seed"] = options["seed"]
    kind = eval_store.effective_scorer(item)
    if kind == "tool" and item.get("tools"):
        payload["tools"] = item["tools"]
    started = time.perf_counter()
    try:
        resp = await _chat_completion(payload)
    except Exception as exc:  # noqa: BLE001 — one bad case is not a dead run
        logger.warning("eval: case %d on %s failed: %s", i, label, exc)
        eval_store.add_result(run_id, label, i, rep=rep, scorer=kind, error=str(exc)[:500])
        return
    latency_ms = (time.perf_counter() - started) * 1000.0
    output, ptok, ctok = _parse_completion(resp)
    tool_calls = _parse_tool_calls(resp)
    if tool_calls and not output:
        output = "\n".join(f"→ {c['name']}({json.dumps(c['args'])})" for c in tool_calls)
    score, scored = score_item(output, item, tool_calls=tool_calls)
    rationale = None
    if kind == "tool":
        _, rationale = score_tool(tool_calls, item.get("expectedTool") or {})
    elif kind == "judge":
        judge = options.get("judge")
        if judge:
            try:
                reply = await _judge_completion(judge_prompt(item, output), judge, model_id)
                score, rationale = parse_judge(reply)
                scored = score is not None
            except Exception as exc:  # noqa: BLE001
                rationale = f"judge failed: {str(exc)[:200]}"
        else:
            rationale = "unscored: no judge configured for this run"
    total = (ptok or 0) + (ctok or 0)
    tps = (ctok / (latency_ms / 1000.0)) if (ctok and latency_ms > 0) else None
    eval_store.add_result(
        run_id, label, i, rep=rep, output=output, score=score, scored=scored, scorer=kind,
        rationale=rationale, tool_calls=tool_calls or None, latency_ms=round(latency_ms, 2),
        prompt_tokens=ptok, completion_tokens=ctok, tokens=total or None,
        tokens_per_sec=round(tps, 2) if tps else None, rss_bytes=_rss_bytes(),
    )
    _emit(run_id, {"type": "result", "model": label, "itemIndex": i, "rep": rep})


async def run_eval(run_id: str) -> None:
    """Execute a run end to end. A model that fails to activate records a
    failure for all its cases instead of crashing the whole run."""
    run = eval_store.get_run(run_id)
    if not run:
        return
    suite = eval_store.get_suite(run["suiteId"])
    if not suite:
        eval_store.set_status(run_id, "error", error="suite was deleted", finished=True)
        return
    items: list[dict] = suite["items"]
    models: list[dict] = run["models"]
    options: dict = run["options"] or {}
    reps = int(options.get("repetitions") or 1)
    ctx = options.get("ctx")
    total_items = len(items) * reps

    snapshot = await _snapshot_active()
    eval_store.set_status(run_id, "running", started=True)
    progress = {"modelIndex": 0, "modelTotal": len(models), "itemIndex": 0, "itemTotal": total_items,
                "currentModel": None, "phase": "running"}
    _publish_progress(run_id, progress)
    try:
        for mi, model in enumerate(models):
            if run_id in _CANCEL:
                break
            path = model.get("path") or ""
            label = model.get("label") or model.get("name") or path
            progress.update(modelIndex=mi, currentModel=label, itemIndex=0, phase="activating")
            _publish_progress(run_id, progress)
            activated = False
            try:
                await _activate(path, ctx=ctx)
                activated = await _wait_healthy()
            except Exception as exc:  # noqa: BLE001
                logger.warning("eval: activating %s failed: %s", label, exc)
            if not activated:
                for i in range(len(items)):
                    eval_store.add_result(run_id, label, i, error="model failed to activate")
                _emit(run_id, {"type": "model_failed", "model": label})
                continue
            model_id = await _active_model_id() or label
            progress.update(phase="running")
            _publish_progress(run_id, progress)
            n = 0
            for rep in range(reps):
                for i, item in enumerate(items):
                    if run_id in _CANCEL:
                        break
                    progress.update(itemIndex=n)
                    _publish_progress(run_id, progress)
                    await _run_case(run_id, label, model_id, item, i, rep, options)
                    n += 1
        canceled = run_id in _CANCEL
        progress.update(phase="canceled" if canceled else "done", modelIndex=len(models), itemIndex=total_items)
        _publish_progress(run_id, progress)
        eval_store.set_status(run_id, "canceled" if canceled else "done", finished=True)
        _emit(run_id, {"type": "done", "status": "canceled" if canceled else "done"})
    except Exception as exc:  # pragma: no cover
        logger.exception("eval run %s crashed", run_id)
        eval_store.set_status(run_id, "error", error=str(exc)[:500], finished=True)
        _emit(run_id, {"type": "done", "status": "error", "error": str(exc)[:500]})
    finally:
        _CANCEL.discard(run_id)
        await _restore(snapshot)
