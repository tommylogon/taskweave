"""Optional OpenAI-compatible LLM helper for task comparison and summary drafting."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from .config import ProjectContext


def resolve_llm_config(ctx: ProjectContext) -> dict[str, Any]:
    """Resolve optional LLM endpoint settings from project config and environment."""
    cfg = ctx.config.get("llm") or {}
    endpoint = str(os.environ.get("TASKWEAVE_LLM_URL") or cfg.get("endpoint") or "").strip().rstrip("/")
    model = str(os.environ.get("TASKWEAVE_LLM_MODEL") or cfg.get("model") or "").strip()
    api_key = str(os.environ.get("TASKWEAVE_LLM_API_KEY") or cfg.get("api_key") or "").strip()
    timeout = float(cfg.get("timeout_seconds", 8.0))
    enabled = bool(cfg.get("enabled", True)) and bool(endpoint)
    return {
        "enabled": enabled,
        "endpoint": endpoint,
        "model": model,
        "api_key": api_key,
        "timeout": max(0.2, timeout),
    }


def _chat_completion(cfg: dict[str, Any], system_prompt: str, user_prompt: str) -> dict[str, Any]:
    if not cfg["enabled"]:
        return {"used": False, "reason": "no LLM endpoint configured", "text": ""}

    base = cfg["endpoint"]
    url = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
    body: dict[str, Any] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }
    if cfg["model"]:
        body["model"] = cfg["model"]

    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        choices = data.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content = str(msg.get("content") or "").strip()
        return {
            "used": True,
            "model": data.get("model") or cfg["model"],
            "text": content,
        }
    except Exception as exc:
        return {"used": False, "reason": f"LLM unavailable: {exc}", "text": ""}


def explain_task_comparison(
    ctx: ProjectContext,
    query_text: str,
    matches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Explain merge, duplicate, or supersede rationale for top task matches."""
    cfg = resolve_llm_config(ctx)
    if not matches:
        return {"used": False, "reason": "no candidate matches to compare", "rationale": ""}
    if not cfg["enabled"]:
        return {"used": False, "reason": "no LLM endpoint configured", "rationale": ""}

    compact_matches = [
        {
            "id": m.get("id"),
            "title": m.get("title"),
            "status": m.get("status"),
            "score": m.get("score"),
            "suggestion": m.get("suggestion"),
        }
        for m in matches[:5]
    ]
    res = _chat_completion(
        cfg,
        "You compare software tasks concisely. Recommend keep-separate, merge, or supersede with 1-3 sentences.",
        f"Candidate/Query:\n{query_text}\n\nExisting matches:\n{json.dumps(compact_matches, indent=2)}",
    )
    return {
        "used": res["used"],
        "model": res.get("model", ""),
        "reason": res.get("reason", ""),
        "rationale": res.get("text", ""),
    }


def draft_task_summary(ctx: ProjectContext, raw_text: str) -> dict[str, Any]:
    """Draft a concise structured summary from messy legacy task notes."""
    cfg = resolve_llm_config(ctx)
    if not cfg["enabled"]:
        return {"used": False, "reason": "no LLM endpoint configured", "summary": ""}
    res = _chat_completion(
        cfg,
        "Summarize the task into a concise 2-sentence 'what' and 'why' description.",
        raw_text[:4000],
    )
    return {
        "used": res["used"],
        "model": res.get("model", ""),
        "reason": res.get("reason", ""),
        "summary": res.get("text", ""),
    }
