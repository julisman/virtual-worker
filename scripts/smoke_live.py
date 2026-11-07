"""Live smoke test — exercises the real stack end to end against a running
service with a real local model (no fakes):

  model activation → agent → chat with a file tool → trace → board task closed
  by the on-duty worker → chat-channel webhook turn → outbound send audited

Usage (from the repo root, with a model already downloaded):

    H=$(mktemp -d); VW_MANAGED_SEARXNG=0 ./vw start --home "$H" --port 8898 --llama-port 8091
    H=$H BASE=http://127.0.0.1:8898 MODEL=Qwen3.5-9B backend/.venv/bin/python -I scripts/smoke_live.py
    ./vw stop --home "$H"

MODEL is a substring of an on-device model's repo or filename. The channel step
posts a signed WhatsApp Cloud API webhook with a fake token, so the final
outbound send is expected to fail (and be audited as failed).
"""
import hashlib, hmac, json, os, sys, time, urllib.request, urllib.error

BASE = os.environ.get("BASE", "http://127.0.0.1:8898")
HOME = os.environ["H"]
WORK = os.path.join(HOME, "work"); os.makedirs(WORK, exist_ok=True)
MODEL_HINT = os.environ.get("MODEL", "Qwen3-8B-4bit")

def req(method, path, body=None, raw=None, headers=None, timeout=120):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            t = resp.read().decode()
            return resp.status, (json.loads(t) if t.startswith(("{", "[")) else t)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:400]

def step(name, ok, detail=""):
    print(("PASS" if ok else "FAIL"), name, ("— " + str(detail)[:300]) if detail else "", flush=True)
    return ok

results = []
# 1. model
st, local = req("GET", "/api/hub/local")
models = local.get("models", []) if isinstance(local, dict) else []
cand = [m for m in models if MODEL_HINT.lower() in ((m.get("repo") or "") + "/" + (m.get("filename") or "")).lower()
        and "mmproj" not in (m.get("filename") or "").lower()]
results.append(step("on-device models listed", bool(models), f"{len(models)} models; picked {cand[0]['repo'] if cand else None}"))
if not cand:
    sys.exit(1)
st, r = req("POST", "/api/runtime/start", {"path": cand[0]["path"], "ctx": 16384, "force": True}, timeout=300)
results.append(step("runtime start accepted", st == 200, str(r)[:200]))
healthy = False
for _ in range(90):
    time.sleep(3)
    st, s = req("GET", "/api/runtime/status")
    if isinstance(s, dict) and s.get("healthy"):
        healthy = True; break
results.append(step("model server healthy", healthy, json.dumps(s)[:200] if isinstance(s, dict) else s))
if not healthy:
    sys.exit(1)

# 2. agent
st, agent = req("POST", "/api/agents", {
    "name": "Smoke Worker", "persona": "You are a careful assistant. Use your tools to do what is asked, then answer briefly.",
    "workingDir": WORK, "role": "assistant", "tools": [],
    "guardrails": {"maxIterations": 8, "channelSend": False}})
results.append(step("agent created", st == 200 and isinstance(agent, dict) and agent.get("id"), str(agent)[:160]))
aid = agent["id"]

# 3. chat with a tool call (SSE)
body = json.dumps({"agentId": aid, "messages": [{"role": "user", "content":
    "Create a file named hello.txt in your working folder containing exactly the text: smoke test ok. Then confirm in one sentence."}]}).encode()
r = urllib.request.Request(BASE + "/api/chat", data=body, method="POST", headers={"Content-Type": "application/json", "x-vw-session": "chat:smoke"})
events = []; text = ""
t0 = time.time()
with urllib.request.urlopen(r, timeout=600) as resp:
    for line in resp:
        line = line.decode("utf-8", "replace").strip()
        if line.startswith("data:"):
            try:
                ev = json.loads(line[5:].strip())
            except ValueError:
                continue
            events.append(ev)
            if ev.get("type") == "text":
                text += ev.get("delta", "")
tools = [e.get("name") for e in events if e.get("type") == "tool_call"]
hello = os.path.join(WORK, "hello.txt")
content = open(hello).read().strip() if os.path.exists(hello) else None
results.append(step("chat run streamed", any(e.get("type") == "done" for e in events), f"{len(events)} events in {time.time()-t0:.0f}s; tools={tools}"))
results.append(step("file tool actually wrote the file", content == "smoke test ok", repr(content)))
results.append(step("reply text present", bool(text.strip()), text.strip()[:140]))

# 4. trace captured
st, dbg = req("GET", "/api/debug/sessions")
sessions = dbg.get("sessions", []) if isinstance(dbg, dict) else []
results.append(step("debug trace recorded for the chat session", any("chat:smoke" in (s.get("id") or s.get("sessionId") or "") for s in sessions), f"{len(sessions)} sessions"))

# 5. board + duty worker
st, task = req("POST", f"/api/agents/{aid}/tasks", {"title": "Write notes", "priority": "high",
    "description": "Create a file named notes.md in the working folder with three bullet points about why local-first AI matters.",
    "criteria": "notes.md exists and has three bullets", "status": "todo"})
results.append(step("task created", st == 200 and isinstance(task, dict), str(task)[:160]))
tid = task.get("id") if isinstance(task, dict) else None
st, d = req("POST", f"/api/agents/{aid}/duty", {"status": "on"})
results.append(step("duty on", st == 200, str(d)[:160]))
final = None
for _ in range(80):
    time.sleep(5)
    st, ts = req("GET", f"/api/agents/{aid}/tasks")
    rows = ts.get("tasks", []) if isinstance(ts, dict) else ts
    me = next((t for t in rows if t.get("id") == tid), None)
    if me and me.get("status") in ("done", "blocked", "review"):
        final = me; break
req("POST", f"/api/agents/{aid}/duty", {"status": "off"})
notes = os.path.join(WORK, "notes.md")
results.append(step("worker closed the task", bool(final) and final.get("status") in ("done", "review"), f"status={final.get('status') if final else 'timeout'}; summary={(final or {}).get('resultSummary','')[:120]}"))
results.append(step("worker produced notes.md", os.path.exists(notes) and open(notes).read().count("-") >= 3 or (os.path.exists(notes) and "*" in open(notes).read()), open(notes).read()[:160] if os.path.exists(notes) else "missing"))
st, act = req("GET", f"/api/agents/{aid}/activity")
acts = act.get("activities", act.get("items", [])) if isinstance(act, dict) else act
results.append(step("activity feed has entries", bool(acts), f"{len(acts)} rows"))

# 6. chat channel end to end (Cloud API webhook → real agent run → send attempt audited)
st, acct = req("POST", "/api/channels/accounts", {"kind": "whatsapp_cloud", "name": "smoke", "enabled": True, "defaultAgentId": aid,
    "config": {"phoneNumberId": "PN1", "dmPolicy": "open", "ackAfterMs": 0, "debounceMs": 0, "runTimeoutS": 300},
    "secrets": {"accessToken": "t", "appSecret": "sec", "verifyToken": "vt"}})
results.append(step("channel account created", st == 200 and acct.get("status", {}).get("state") == "open", str(acct.get("status"))[:160] if isinstance(acct, dict) else acct))
payload = json.dumps({"entry": [{"changes": [{"field": "messages", "value": {"metadata": {"phone_number_id": "PN1"},
    "contacts": [{"wa_id": "628111", "profile": {"name": "Smoke Tester"}}],
    "messages": [{"from": "628111", "id": "wamid.smoke1", "timestamp": "1", "type": "text", "text": {"body": "Reply with exactly the single word PONG and nothing else."}}]}}]}]}).encode()
sig = "sha256=" + hmac.new(b"sec", payload, hashlib.sha256).hexdigest()
st, wh = req("POST", f"/api/channels/webhook/whatsapp_cloud/{acct['id']}", raw=payload, headers={"x-hub-signature-256": sig})
results.append(step("webhook accepted", st == 200 and wh.get("messages") == 1, str(wh)[:120]))
sess = None
for _ in range(60):
    time.sleep(3)
    st, s = req("GET", f"/api/sessions/whatsapp_cloud:{acct['id']}:628111")
    if st == 200 and isinstance(s, dict) and any(m.get("role") == "assistant" for m in s.get("messages", [])):
        sess = s; break
reply = next((m["content"] for m in (sess or {}).get("messages", []) if m.get("role") == "assistant"), None)
results.append(step("channel turn ran the agent and persisted the reply", bool(reply), repr(reply)[:120]))
st, dels = req("GET", "/api/deliveries?limit=5")
rows = dels.get("deliveries", []) if isinstance(dels, dict) else []
last = next((d for d in rows if d.get("channel") == "chat:whatsapp_cloud"), None)
results.append(step("outbound send audited (expected to fail: fake token, no network)", bool(last), f"status={last.get('status') if last else None} error={(last or {}).get('error','')[:100]}"))

print("\n==== SUMMARY:", sum(1 for r in results if r), "passed /", len(results), "steps")
sys.exit(0 if all(results) else 2)
