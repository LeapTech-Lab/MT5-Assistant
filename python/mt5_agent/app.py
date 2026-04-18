from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

import time
AI_CALL_MIN_INTERVAL = float(os.getenv("AI_CALL_MIN_INTERVAL", "10"))
_last_ai_call_time: float = 0.0

from dotenv import load_dotenv
load_dotenv()  # ← 加在所有 import os / os.getenv 之前
logger = logging.getLogger("mt5_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

API_KEY = os.getenv("BRIDGE_API_KEY", "change_me")
AI_PROVIDER = os.getenv("AI_PROVIDER", "openai_compatible").lower()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4.1-mini")
AI_TIMEOUT_SECONDS = int(os.getenv("AI_TIMEOUT_SECONDS", "20"))
OPENAI_PATH = os.getenv("OPENAI_PATH", "/chat/completions")
ANTHROPIC_VERSION = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
GEMINI_PATH_TEMPLATE = os.getenv("GEMINI_PATH_TEMPLATE", "/v1beta/models/{model}:generateContent")
MAX_RISK_PERCENT = float(os.getenv("MAX_RISK_PERCENT", "1.0"))
DEFAULT_AGENT_MODE = os.getenv("DEFAULT_AGENT_MODE", "user")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
REVIEW_FILE = DATA_DIR / "trade_review.md"
STYLE_FILE = DATA_DIR / "style_profile.json"

app = FastAPI(title="MT5 AI Bridge")


# ─────────────────────────────────────────────────────────────
# ✅ 422 详情日志：开发期间可见具体是哪个字段验证失败
# ─────────────────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    logger.error("422 Validation Error on %s %s", request.method, request.url.path)
    logger.error("Errors: %s", exc.errors())
    logger.error("Raw body (first 500 chars): %s", body[:500].decode("utf-8", errors="replace"))
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body_preview": body[:200].decode("utf-8", errors="replace")},
    )


# ─────────────────────────────────────────────────────────────
# Pydantic Models
# ─────────────────────────────────────────────────────────────

class Position(BaseModel):
    ticket: int
    type: int
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float


class Candle(BaseModel):
    time: str
    open: float
    high: float
    low: float
    close: float
    # ✅ 修复：改为 float（部分 broker 的 BTCUSD tick_volume 为小数）
    tick_volume: float = 0.0
    # ✅ 新增：EA 现在会发送 real_volume，Python 端接收并容错
    real_volume: Optional[float] = None

    # ✅ 兼容性校验：如果 tick_volume 是负数或异常值，强制为 0
    @field_validator("tick_volume", mode="before")
    @classmethod
    def coerce_tick_volume(cls, v):
        try:
            val = float(v)
            return max(val, 0.0)
        except (TypeError, ValueError):
            return 0.0

    model_config = {"extra": "ignore"}  # 忽略 EA 发来的多余字段，不报错


class Snapshot(BaseModel):
    symbol: str
    bid: float
    ask: float
    time: str
    positions: list[Position] = Field(default_factory=list)
    candles_m1: list[Candle] = Field(default_factory=list)

    model_config = {"extra": "ignore"}

    @field_validator("candles_m1", mode="before")
    @classmethod
    def validate_candles(cls, v):
        if not isinstance(v, list):
            return []
        return v


class TradeCommand(BaseModel):
    action: Literal[
        "none",
        "buy_market",
        "sell_market",
        "buy_limit",
        "sell_limit",
        "buy_stop",
        "sell_stop",
    ] = "none"
    volume: float = 0.01
    sl: float = 0.0
    tp: float = 0.0
    price: float = 0.0
    reason: str = ""


class ChatReq(BaseModel):
    message: str
    symbol: str = "XAUUSD"


class ModeUpdateReq(BaseModel):
    mode: Literal["kernel", "user"]
    reason: str = "manual switch"


@dataclass
class RuntimeState:
    last_snapshot: Snapshot | None = None
    next_command: TradeCommand = field(default_factory=TradeCommand)
    mode: Literal["kernel", "user"] = "user"


runtime = RuntimeState(mode="kernel" if DEFAULT_AGENT_MODE == "kernel" else "user")


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _auth(x_api_key: str | None) -> None:
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _candlestick_summary(candles: list[Candle]) -> dict:
    if len(candles) < 3:
        return {"pattern": "unknown", "trend": "unknown"}

    c1, c2, c3 = candles[-1], candles[-2], candles[-3]
    trend = "up" if c1.close > c3.close else "down"
    body = abs(c1.close - c1.open)
    wick = c1.high - c1.low
    ratio = body / wick if wick > 0 else 0

    if ratio < 0.2:
        pattern = "doji"
    elif c1.close > c1.open and c2.close < c2.open and c1.close > c2.open:
        pattern = "bullish_engulfing"
    elif c1.close < c1.open and c2.close > c2.open and c1.close < c2.open:
        pattern = "bearish_engulfing"
    else:
        pattern = "normal"

    return {"pattern": pattern, "trend": trend, "last_close": c1.close}


def _risk_guard(snapshot: Snapshot, cmd: TradeCommand) -> TradeCommand:
    if cmd.action == "none":
        return cmd

    if cmd.volume < 0.01:
        cmd.volume = 0.01

    if cmd.sl <= 0 or cmd.tp <= 0:
        return TradeCommand(action="none", reason="Missing SL/TP rejected by risk guard")

    account_equity = 10000.0
    max_loss = account_equity * (MAX_RISK_PERCENT / 100)
    if abs(cmd.price - cmd.sl) * cmd.volume * 100 > max_loss:
        return TradeCommand(action="none", reason="Risk too large")

    return cmd


def _extract_first_json_block(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start : end + 1]


def _normalize_trade_command(raw_text: str) -> TradeCommand:
    cleaned = _extract_first_json_block(raw_text)
    if not cleaned:
        return TradeCommand(action="none", reason="Model output has no JSON command")

    try:
        return TradeCommand.model_validate_json(cleaned)
    except Exception:
        return TradeCommand(action="none", reason="Invalid command JSON from model")


def _build_ai_payload(snapshot: Snapshot, user_message: str = "") -> dict:
    pattern = _candlestick_summary(snapshot.candles_m1)
    style = _load_json(STYLE_FILE, {"risk_preference": "conservative"})
    return {
        "mode": runtime.mode,
        "snapshot": snapshot.model_dump(),
        "pattern": pattern,
        "style": style,
        "review_notes": REVIEW_FILE.read_text(encoding="utf-8") if REVIEW_FILE.exists() else "",
        "user_message": user_message,
        "required_json_schema": TradeCommand.model_json_schema(),
    }


# ─────────────────────────────────────────────────────────────
# AI Provider Calls
# ─────────────────────────────────────────────────────────────

async def _call_openai_compatible(client: httpx.AsyncClient, prompt_json: str) -> TradeCommand:
    body = {
        "model": AI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": "You are an execution-focused trading copilot. Reply only in JSON.",
            },
            {"role": "user", "content": prompt_json},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "trade_command", "schema": TradeCommand.model_json_schema()},
        },
    }

    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    resp = await client.post(f"{AI_BASE_URL.rstrip('/')}{OPENAI_PATH}", headers=headers, json=body)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return _normalize_trade_command(content)


async def _call_anthropic(client: httpx.AsyncClient, prompt_json: str) -> TradeCommand:
    body = {
        "model": AI_MODEL,
        "max_tokens": 600,
        "system": "You are an execution-focused trading copilot. Reply only in JSON.",
        "messages": [{"role": "user", "content": prompt_json}],
    }
    headers = {
        "x-api-key": AI_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "Content-Type": "application/json",
    }

    resp = await client.post(f"{AI_BASE_URL.rstrip('/')}/v1/messages", headers=headers, json=body)
    resp.raise_for_status()
    blocks = resp.json().get("content", [])
    text = "\n".join(block.get("text", "") for block in blocks if block.get("type") == "text")
    return _normalize_trade_command(text)


async def _call_gemini(client: httpx.AsyncClient, prompt_json: str) -> TradeCommand:
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt_json}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    path = GEMINI_PATH_TEMPLATE.format(model=AI_MODEL)
    url = f"{AI_BASE_URL.rstrip('/')}{path}?key={AI_API_KEY}"
    resp = await client.post(url, json=body)
    resp.raise_for_status()
    candidates = resp.json().get("candidates", [])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "\n".join(part.get("text", "") for part in parts)
    return _normalize_trade_command(text)


# async def _call_ai(snapshot: Snapshot, user_message: str = "") -> TradeCommand:
#     if not AI_API_KEY:
#         return TradeCommand(action="none", reason="AI_API_KEY missing")
# 
#     prompt_json = json.dumps(_build_ai_payload(snapshot, user_message), ensure_ascii=False)
# 
#     async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as client:
#         if AI_PROVIDER in {"openai", "openai_compatible", "deepseek", "moonshot", "qwen", "siliconflow"}:
#             cmd = await _call_openai_compatible(client, prompt_json)
#         elif AI_PROVIDER == "anthropic":
#             cmd = await _call_anthropic(client, prompt_json)
#         elif AI_PROVIDER in {"gemini", "google"}:
#             cmd = await _call_gemini(client, prompt_json)
#         else:
#             return TradeCommand(action="none", reason=f"Unsupported AI_PROVIDER: {AI_PROVIDER}")
# 
#     return _risk_guard(snapshot, cmd)
# 
async def _call_ai(snapshot: Snapshot, user_message: str = "") -> TradeCommand:
    global _last_ai_call_time

    if not AI_API_KEY:
        return TradeCommand(action="none", reason="AI_API_KEY missing")

    # 限流：距离上次调用不足间隔，直接跳过
    now = time.time()
    elapsed = now - _last_ai_call_time
    if elapsed < AI_CALL_MIN_INTERVAL:
        wait = AI_CALL_MIN_INTERVAL - elapsed
        logger.info("Rate limit guard: skip AI call, next in %.1fs", wait)
        return TradeCommand(action="none", reason=f"Rate limit guard, retry in {wait:.0f}s")

    _last_ai_call_time = now
    prompt_json = json.dumps(_build_ai_payload(snapshot, user_message), ensure_ascii=False)

    try:
        async with httpx.AsyncClient(timeout=AI_TIMEOUT_SECONDS) as client:
            if AI_PROVIDER in {"openai", "openai_compatible", "deepseek", "moonshot", "qwen", "siliconflow"}:
                cmd = await _call_openai_compatible(client, prompt_json)
            elif AI_PROVIDER == "anthropic":
                cmd = await _call_anthropic(client, prompt_json)
            elif AI_PROVIDER in {"gemini", "google"}:
                cmd = await _call_gemini(client, prompt_json)
            else:
                return TradeCommand(action="none", reason=f"Unsupported AI_PROVIDER: {AI_PROVIDER}")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            # 429 时退避，下次至少等 30 秒
            _last_ai_call_time = time.time() + 30
            logger.warning("429 Too Many Requests, backing off 30s")
            return TradeCommand(action="none", reason="API rate limited, backing off")
        logger.error("AI HTTP error %s: %s", e.response.status_code, e)
        return TradeCommand(action="none", reason=f"AI HTTP error {e.response.status_code}")
    except Exception as e:
        logger.error("AI call failed: %s", e)
        return TradeCommand(action="none", reason=f"AI call exception: {type(e).__name__}")

    return _risk_guard(snapshot, cmd)


def _persist_state() -> None:
    snapshot_dump = runtime.last_snapshot.model_dump() if runtime.last_snapshot else None
    state = {
        "last_update": datetime.now(timezone.utc).isoformat(),
        "mode": runtime.mode,
        "snapshot": snapshot_dump,
        "next_command": runtime.next_command.model_dump(),
    }
    _save_json(STATE_FILE, state)


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@app.post("/v1/agent/mode")
async def set_mode(req: ModeUpdateReq, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    runtime.mode = req.mode
    runtime.next_command = TradeCommand(action="none", reason=f"Switched to {req.mode} mode")

    line = (
        f"- {datetime.now(timezone.utc).isoformat()} mode_switch: "
        f"{json.dumps(req.model_dump(), ensure_ascii=False)}\n"
    )
    with REVIEW_FILE.open("a", encoding="utf-8") as f:
        f.write(line)

    _persist_state()
    return {"ok": True, "mode": runtime.mode}


@app.get("/v1/agent/mode")
async def get_mode(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return {"mode": runtime.mode}


@app.post("/v1/mt5/ingest")
async def ingest(snapshot: Snapshot, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    runtime.last_snapshot = snapshot

    logger.info(
        "Ingest OK | symbol=%s bid=%.5f ask=%.5f candles=%d positions=%d",
        snapshot.symbol,
        snapshot.bid,
        snapshot.ask,
        len(snapshot.candles_m1),
        len(snapshot.positions),
    )

    if runtime.mode == "kernel":
        cmd = await _call_ai(snapshot)
    else:
        cmd = TradeCommand(action="none", reason="User mode: suggestions only, no auto-trading")

    runtime.next_command = cmd
    _persist_state()
    return {"ok": True, "mode": runtime.mode, "command": cmd}


@app.get("/v1/mt5/next-command")
async def next_command(symbol: str, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    if runtime.last_snapshot is None or runtime.last_snapshot.symbol != symbol:
        return TradeCommand(action="none", reason="No snapshot yet")

    if runtime.mode != "kernel":
        return TradeCommand(action="none", reason="User mode: command execution disabled")

    cmd = runtime.next_command
    runtime.next_command = TradeCommand(action="none", reason="Command consumed")
    _persist_state()
    return cmd


@app.post("/v1/mt5/order-result")
async def order_result(payload: dict, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    line = f"- {datetime.now(timezone.utc).isoformat()} result: {json.dumps(payload, ensure_ascii=False)}\n"
    with REVIEW_FILE.open("a", encoding="utf-8") as f:
        f.write(line)
    logger.info("Order result: %s", payload)
    return {"ok": True}


@app.post("/v1/chat")
async def chat(req: ChatReq):
    snapshot = runtime.last_snapshot
    if snapshot is None:
        return {"answer": "尚未收到 MT5 数据，请先启动 EA。"}

    cmd = await _call_ai(snapshot, req.message)
    return {
        "mode": runtime.mode,
        "answer": f"建议动作: {cmd.action}, 手数: {cmd.volume}, SL: {cmd.sl}, TP: {cmd.tp}, 原因: {cmd.reason}",
        "command": cmd,
        "executable": runtime.mode == "kernel",
        "pattern": _candlestick_summary(snapshot.candles_m1),
        "provider": AI_PROVIDER,
    }


@app.get("/health")
async def health():
    snap = runtime.last_snapshot
    return {
        "status": "ok",
        "mode": runtime.mode,
        "provider": AI_PROVIDER,
        "last_symbol": snap.symbol if snap else None,
        "last_candles": len(snap.candles_m1) if snap else 0,
    }
