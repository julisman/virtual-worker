"""Local fine-tuning (runtime/finetune_manager + routes/finetune).

Hermetic: no MLX. The trainer interpreter is replaced by a fake script that
prints mlx-lm style loss lines and creates the fused output dir, so the whole
job lifecycle (prepare → run → fuse → done, cancel, persistence) runs for real.
"""

import asyncio
import json
import stat

import pytest
from fastapi.testclient import TestClient

import config
from runtime import finetune_manager as ft


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "APP_HOME", tmp_path)
    monkeypatch.setattr(ft, "_HF_CACHE", tmp_path / "hf-empty")
    monkeypatch.setattr(ft, "_loaded_home", None)
    ft._JOBS.clear()
    ft._PROCS.clear()
    ft._SUBS.clear()
    yield tmp_path
    ft._JOBS.clear()


def _chat_rows(n=12):
    return "".join(json.dumps({"messages": [{"role": "user", "content": f"q{i}"},
                                            {"role": "assistant", "content": f"a{i}"}]}) + "\n"
                   for i in range(n)).encode()


# ------------------------------------------------------------ trainable ----

def test_trainable_classification():
    assert ft._trainable({"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2"})[::2] == (True, "mlx-lm")
    # newer text-only families use *ForConditionalGeneration
    assert ft._trainable({"architectures": ["Qwen3_5ForConditionalGeneration"],
                          "model_type": "qwen3_5"})[::2] == (True, "mlx-lm")
    assert ft._trainable({"architectures": ["Qwen2VLForConditionalGeneration"], "model_type": "qwen2_vl",
                          "vision_config": {}})[::2] == (True, "mlx-vlm")
    for mtype, arch in (("t5", "T5ForConditionalGeneration"), ("whisper", "WhisperForConditionalGeneration"),
                        ("bert", "BertModel"), ("qwen3_tts", "Qwen3TTSForConditionalGeneration")):
        assert ft._trainable({"architectures": [arch], "model_type": mtype})[0] is False


def test_base_models_lists_fused_models_under_local_namespace(home):
    d = home / "models" / "rani-v1"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"architectures": ["Qwen2ForCausalLM"], "model_type": "qwen2",
                                               "quantization": {"bits": 4}}))
    (d / "model.safetensors").write_bytes(b"\0" * 8)
    (home / "models" / "no-weights").mkdir()
    ((home / "models" / "no-weights") / "config.json").write_text("{}")
    models = ft.list_base_models()
    assert len(models) == 1
    m = models[0]
    assert m["repo"] == "local/rani-v1" and m["trainable"] and m["quant"] == "4-bit" and m["trainer"] == "mlx-lm"


def test_partial_download_is_not_trainable(home):
    d = home / "models" / "half"
    d.mkdir(parents=True)
    (d / "config.json").write_text(json.dumps({"architectures": ["Qwen3MoeForCausalLM"], "model_type": "qwen3_moe"}))
    (d / "model-00001-of-00002.safetensors").write_bytes(b"\0")
    (d / "model.safetensors.index.json").write_text(json.dumps({"weight_map": {
        "a": "model-00001-of-00002.safetensors", "b": "model-00002-of-00002.safetensors"}}))
    m = ft.list_base_models()[0]
    assert m["trainable"] is False and "incomplete download (1" in m["reason"]
    (d / "model-00002-of-00002.safetensors").write_bytes(b"\0")
    assert ft.list_base_models()[0]["trainable"] is True
    (d / "model.safetensors.index.json").unlink()            # no index: shard names still tell
    (d / "model-00002-of-00002.safetensors").unlink()
    assert "incomplete download" in ft.list_base_models()[0]["reason"]


# ------------------------------------------------------------- datasets ----

def test_dataset_validation_and_crud(home):
    rec = ft.save_dataset("rani.jsonl", _chat_rows(3))
    assert rec["examples"] == 3 and rec["format"] == "chat" and rec["source"] == "upload"
    assert [d["id"] for d in ft.list_datasets()] == [rec["id"]]
    assert ft.get_dataset(rec["id"])["name"] == "rani.jsonl"
    for bad, msg in ((b"not json\n", "invalid JSON"), (b'{"foo": 1}\n', "unsupported row"),
                     (b'{"text":"a"}\n{"prompt":"p","completion":"c"}\n', "mixed formats"), (b"\n", "empty")):
        with pytest.raises(ValueError, match=msg):
            ft.save_dataset("bad.jsonl", bad)
    assert len(ft.list_datasets()) == 1                      # rejected uploads leave nothing behind
    assert ft.get_dataset("../../etc/passwd") is None
    assert ft.delete_dataset(rec["id"]) and ft.list_datasets() == []


def test_split_keeps_a_batch_in_both_halves(tmp_path):
    src = tmp_path / "d.jsonl"
    src.write_bytes(_chat_rows(20))
    ft._split(str(src), tmp_path / "out", batch_size=1)
    assert len((tmp_path / "out" / "valid.jsonl").read_text().splitlines()) == 2
    assert len((tmp_path / "out" / "train.jsonl").read_text().splitlines()) == 18
    src.write_bytes(_chat_rows(2))                            # tiny: both splits share everything
    ft._split(str(src), tmp_path / "tiny", batch_size=2)
    assert len((tmp_path / "tiny" / "train.jsonl").read_text().splitlines()) == 2


def test_parse_line_reads_both_trainer_formats():
    job = {"id": "ft_p0000001", "config": {"iters": 4}, "metrics": [], "logTail": [], "progress": {"iter": 0, "total": 4}}
    ft.parse_line(job, "Iter 2: Train loss 1.500, Learning Rate 2e-4")
    ft.parse_line(job, "Iter 2: Val loss 1.700")
    ft.parse_line(job, "Training:  50%|█████     | 2/4 [00:05<00:04]Training:  75%|███████▌  | 3/4 [00:05<00:01]")
    assert job["progress"] == {"iter": 3, "total": 4}
    ft.parse_line(job, "optimizer_step=4, train_loss=2.9428, train_nll=2.9428")
    ft.parse_line(job, "Training: 75%|███ | 3/4 [00:05<00:01, loss=2.94]    Validation (iteration 4):")
    ft.parse_line(job, "val_loss=0.53321, val_nll=0.53321, val_token_accuracy=0.86538")
    assert job["metrics"] == [{"iter": 2, "trainLoss": 1.5}, {"iter": 2, "valLoss": 1.7},
                              {"iter": 4, "trainLoss": 2.9428}, {"iter": 4, "valLoss": 0.53321}]
    assert job["progress"] == {"iter": 4, "total": 4}


def test_config_yaml_is_quoted_and_cadences_fit(tmp_path):
    cfg = {"baseModel": 'my "odd" model', "iters": 5, "numLayers": 8, "batchSize": 1, "learningRate": 2e-4,
           "maxSeqLength": 2048, "loraRank": 8, "loraAlpha": 16, "loraDropout": 0.0}
    text = ft._write_config(tmp_path, cfg).read_text()
    assert 'model: "my \\"odd\\" model"' in text
    assert "steps_per_eval: 5" in text and "save_every: 5" in text and "  rank: 8" in text


# ----------------------------------------------------------------- jobs ----

def _fake_trainer(home, fail=False, sleep=0):
    """A stand-in interpreter: prints loss lines; on fuse, creates --save-path."""
    script = home / "fakepy"
    script.write_text(f"""#!/bin/sh
sleep {sleep}
for a in "$@"; do
  if [ "$prev" = "--save-path" ]; then mkdir -p "$a"; echo '{{}}' > "$a/config.json"; fi
  prev="$a"
done
echo "Iter 1: Train loss 2.500, Learning Rate 2e-4"
echo "Iter 1: Val loss 2.600"
echo "Iter 2: Train loss 1.250"
exit {1 if fail else 0}
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


async def _wait(job, statuses=ft.FINISHED, timeout=10.0):
    for _ in range(int(timeout / 0.02)):
        if job["status"] in statuses:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"job stuck in {job['status']}")


def test_job_runs_to_done_and_fuses_into_models(home, monkeypatch):
    monkeypatch.setattr(ft, "mlx_installed", lambda: True)
    monkeypatch.setattr(ft, "_mlx_python", lambda: _fake_trainer(home))
    ds = ft.save_dataset("rani.jsonl", _chat_rows(12))

    async def go():
        job = await ft.start({"baseModel": "/m/base", "datasetId": ds["id"], "iters": 2, "name": "Rani v1"})
        await _wait(job)
        return job
    job = asyncio.run(go())
    assert job["status"] == "done", job.get("error")
    assert job["outputPath"] == str(home / "models" / "Rani-v1")
    assert [m for m in job["metrics"] if "trainLoss" in m] == [{"iter": 1, "trainLoss": 2.5}, {"iter": 2, "trainLoss": 1.25}]
    assert {"iter": 1, "valLoss": 2.6} in job["metrics"] and job["progress"] == {"iter": 2, "total": 2}
    assert (home / "finetune" / "jobs" / job["id"] / "train.log").exists()
    # persisted and reloadable
    ft._JOBS.clear(); ft._loaded_home = None
    assert ft.get_job(job["id"])["status"] == "done"
    # delete reclaims the job dir and the fused model
    out = ft.delete(job["id"])
    assert out["deleted"] == job["id"] and not (home / "models" / "Rani-v1").exists()


def test_failed_training_reports_error(home, monkeypatch):
    monkeypatch.setattr(ft, "mlx_installed", lambda: True)
    monkeypatch.setattr(ft, "_mlx_python", lambda: _fake_trainer(home, fail=True))
    ds = ft.save_dataset("d.jsonl", _chat_rows(4))

    async def go():
        job = await ft.start({"baseModel": "/m/base", "datasetId": ds["id"], "iters": 2})
        await _wait(job)
        return job
    job = asyncio.run(go())
    assert job["status"] == "error" and "code 1" in job["error"]


def test_one_job_at_a_time_and_cancel(home, monkeypatch):
    monkeypatch.setattr(ft, "mlx_installed", lambda: True)
    monkeypatch.setattr(ft, "_mlx_python", lambda: _fake_trainer(home, sleep=30))
    ds = ft.save_dataset("d.jsonl", _chat_rows(4))

    async def go():
        job = await ft.start({"baseModel": "/m/base", "datasetId": ds["id"], "iters": 2})
        await _wait(job, ("running",))
        with pytest.raises(RuntimeError, match="already active"):
            await ft.start({"baseModel": "/m/base", "datasetId": ds["id"]})
        with pytest.raises(ValueError, match="still active"):
            ft.delete(job["id"])
        for _ in range(100):                                  # wait for the child to spawn
            if job["id"] in ft._PROCS:
                break
            await asyncio.sleep(0.02)
        await ft.cancel(job["id"])
        await _wait(job)
        return job
    assert asyncio.run(go())["status"] == "cancelled"


def test_cancel_before_the_trainer_starts_sticks(home, monkeypatch):
    """A cancel while queued (no process yet) must not be overwritten by the run loop."""
    monkeypatch.setattr(ft, "mlx_installed", lambda: True)
    monkeypatch.setattr(ft, "_mlx_python", lambda: _fake_trainer(home))
    ds = ft.save_dataset("d.jsonl", _chat_rows(4))

    async def go():
        job = await ft.start({"baseModel": "/m/base", "datasetId": ds["id"], "iters": 2})
        await ft.cancel(job["id"])          # before the _run task has had a turn
        await asyncio.sleep(0.3)
        return job
    job = asyncio.run(go())
    assert job["status"] == "cancelled" and not job["logTail"]


def test_cancel_between_training_and_fuse(home, monkeypatch):
    """No live process at that instant: the next step must see the cancel."""
    monkeypatch.setattr(ft, "mlx_installed", lambda: True)
    monkeypatch.setattr(ft, "_mlx_python", lambda: _fake_trainer(home))
    ds = ft.save_dataset("d.jsonl", _chat_rows(4))
    real = ft._finish_if_stopped

    async def go():
        job = None

        def hook(j, rc, what):
            out = real(j, rc, what)
            if what == "training" and not out:
                asyncio.get_running_loop().create_task(ft.cancel(j["id"]))
            return out
        monkeypatch.setattr(ft, "_finish_if_stopped", hook)
        job = await ft.start({"baseModel": "/m/base", "datasetId": ds["id"], "iters": 2})
        await _wait(job)
        return job
    job = asyncio.run(go())
    assert job["status"] == "cancelled"
    assert not list((home / "models").glob("*"))  # fuse never ran


def test_shutdown_stops_a_running_trainer(home, monkeypatch):
    monkeypatch.setattr(ft, "mlx_installed", lambda: True)
    monkeypatch.setattr(ft, "_mlx_python", lambda: _fake_trainer(home, sleep=30))
    ds = ft.save_dataset("d.jsonl", _chat_rows(4))

    async def go():
        job = await ft.start({"baseModel": "/m/base", "datasetId": ds["id"], "iters": 2})
        for _ in range(250):
            if job["id"] in ft._PROCS:
                break
            await asyncio.sleep(0.02)
        proc = ft._PROCS[job["id"]]
        await ft.shutdown()
        return proc
    assert asyncio.run(go()).returncode is not None


def test_start_validation(home, monkeypatch):
    monkeypatch.setattr(ft, "mlx_installed", lambda: False)
    with pytest.raises(RuntimeError, match="not installed"):
        asyncio.run(ft.start({"baseModel": "x", "datasetId": "ds_00000000"}))
    monkeypatch.setattr(ft, "mlx_installed", lambda: True)
    with pytest.raises(ValueError, match="baseModel"):
        asyncio.run(ft.start({"datasetId": "ds_00000000"}))
    with pytest.raises(ValueError, match="dataset"):
        asyncio.run(ft.start({"baseModel": "x", "datasetId": "ds_00000000"}))
    ds = ft.save_dataset("d.jsonl", _chat_rows(4))
    with pytest.raises(ValueError, match="learningRate"):
        asyncio.run(ft.start({"baseModel": "x", "datasetId": ds["id"], "learningRate": "fast"}))


def test_interrupted_job_on_reload(home):
    d = home / "finetune" / "jobs" / "ft_dead0001"
    d.mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({"id": "ft_dead0001", "status": "running", "createdAt": 1,
                                            "name": "x", "config": {}}))
    assert ft.get_job("ft_dead0001")["status"] == "interrupted"


def test_delete_never_touches_paths_outside_its_dirs(home):
    outside = home / "precious"
    outside.mkdir()
    (home / "finetune" / "jobs" / "ft_x0000001").mkdir(parents=True)
    ft._loaded_home = config.APP_HOME
    ft._JOBS["ft_x0000001"] = {"id": "ft_x0000001", "status": "done", "outputPath": str(outside), "config": {}}
    ft.delete("ft_x0000001")
    assert outside.exists()


# --------------------------------------------------------------- routes ----

def test_routes_and_feedback_dataset(home):
    from app import app
    from storage import feedback_store
    c = TestClient(app)
    r = c.post("/api/finetune/datasets/feedback", json={})
    assert r.status_code == 400 and "no usable feedback" in r.json()["detail"]
    feedback_store.record({"sessionId": "s", "messageId": "1", "signal": "up",
                           "userPrompt": "Bisa antar ke Canggu?", "original": "Bisa Kak 🛵"})
    feedback_store.record({"sessionId": "s", "messageId": "2", "signal": "edit", "userPrompt": "Harga?",
                           "original": "Rp 50.000", "correctedText": "Harga pastinya via WA ya Kak"})
    feedback_store.record({"sessionId": "s", "messageId": "3", "signal": "down", "userPrompt": "x", "original": "y"})
    r = c.post("/api/finetune/datasets/feedback", json={"name": "rani-feedback"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["examples"] == 2 and body["positives"] == 1 and body["corrections"] == 1
    assert body["name"] == "rani-feedback.jsonl" and body["source"] == "feedback"
    assert [d["id"] for d in c.get("/api/finetune/datasets").json()["datasets"]] == [body["id"]]
    up = c.post("/api/finetune/datasets", files={"file": ("x.jsonl", b"nope\n", "application/jsonl")})
    assert up.status_code == 400
    assert c.get("/api/finetune/jobs").json() == {"jobs": []}
    assert c.get("/api/finetune/jobs/ft_nope").status_code == 404
    assert c.delete(f"/api/finetune/datasets/{body['id']}").status_code == 200


def test_finetune_is_admin_in_team_mode():
    from middleware.team_auth import required_role
    assert required_role("POST", "/api/finetune/jobs") == "admin"
    assert required_role("POST", "/api/feedback") == "member"
