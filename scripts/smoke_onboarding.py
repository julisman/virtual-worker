"""Headless walkthrough of the first-run wizard against a running service.

Needs the backend venv (Playwright + Chromium) and a model already on disk whose
repo name contains MODEL (default: Qwen3.5-9B-MLX-4bit). Usage:

    H=$(mktemp -d); VW_MANAGED_SEARXNG=0 ./vw start --home "$H" --port 8898 --llama-port 8091
    BASE=http://127.0.0.1:8898 MODEL=Qwen3.5-9B-MLX-4bit backend/.venv/bin/python scripts/smoke_onboarding.py
    ./vw stop --home "$H"

Steps: wizard shown → activate the on-disk model → deploy "Personal Assistant"
→ skip the chat app → finish → send one chat message and wait for the reply.
"""

import json
import os
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE", "http://127.0.0.1:8898")
MODEL = os.environ.get("MODEL", "Qwen3.5-9B-MLX-4bit")
SHOTS = os.environ.get("SHOTS", "")


def status():
    return json.load(urllib.request.urlopen(BASE + "/api/onboarding/status"))


def shot(page, name):
    if SHOTS:
        page.screenshot(path=os.path.join(SHOTS, name))


with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page(viewport={"width": 1280, "height": 900})
    page.goto(BASE + "/", wait_until="networkidle")
    page.wait_for_selector("text=Welcome to Virtual Worker", timeout=15000)
    wiz = page.locator("section", has_text="Pick a model").first
    assert wiz.count() == 1, "wizard not shown"
    shot(page, "onb-1-model.png")
    wiz.locator("button", has_text="Use a model already on disk").click()
    row = wiz.locator("div.flex.items-center", has_text=MODEL).first
    row.wait_for(timeout=15000)
    row.locator("button", has_text="Activate").click()
    page.wait_for_selector("text=Hire your first worker", timeout=240000)
    assert json.load(urllib.request.urlopen(BASE + "/api/runtime/status"))["healthy"]
    print("model activated")
    wiz2 = page.locator("section", has_text="Hire your first worker").first
    wiz2.locator("button", has_text="Personal Assistant").first.wait_for(timeout=15000)
    wiz2.locator("button", has_text="Personal Assistant").first.click()
    wiz2.locator("input[placeholder*='so the worker knows']").fill("Owner")
    wiz2.locator("button", has_text="Deploy worker").click()
    page.wait_for_selector("text=Message it from your phone", timeout=30000)
    names = [a["name"] for a in json.load(urllib.request.urlopen(BASE + "/api/agents"))["agents"]]
    assert "Personal Assistant" in names, names
    print("worker deployed")
    wiz3 = page.locator("section", has_text="Message it from your phone").first
    wiz3.locator("button", has_text="Later").click()
    wiz3.locator("button", has_text="Continue").click()
    page.wait_for_selector("text=You're set", timeout=15000)
    page.locator("button", has_text="Open chat").click()
    page.wait_for_timeout(2000)
    st = status()
    assert st["dismissed"] and not st["needed"], st
    ta = page.locator("textarea").first
    ta.fill("Reply with exactly: ONBOARDING OK")
    ta.press("Enter")
    page.wait_for_selector("text=ONBOARDING OK", timeout=120000)
    shot(page, "onb-5-chat.png")
    print("chat reply received")
    b.close()
print("ONBOARDING OK")
