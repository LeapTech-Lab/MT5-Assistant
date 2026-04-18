from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

API_KEY = os.getenv("BRIDGE_API_KEY", "change_me")
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.openai.com/v1")
AI_API_KEY = os.getenv("AI_API_KEY", "")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4.1-mini")
MAX_RISK_PERCENT = float(os.getenv("MAX_RISK_PERCENT", "1.0"))
DEFAULT_AGENT_MODE = os.getenv("DEFAULT_AGENT_MODE", "user")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = DATA_DIR / "state.json"
REVIEW_FILE = DATA_DIR / "trade_review.md"
STYLE_FILE = DATA_DIR / "style_profile.json"

app = FastAPI(title="MT5 AI Bridge")


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
    tick_volume: int


class Snapshot(BaseModel):
    symbol: str
    bid: float
    ask: float
    time: str
    positions: list[Position] = Field(default_factory=list)
    candles_m1: list[Candle] = Field(default_factory=list)


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
    next_command: TradeCommand = TradeCommand()
    mode: Literal["kernel", "user"] = "user"


runtime = RuntimeState(mode="kernel" if DEFAULT_AGENT_MODE == "kernel" else "user")


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


async def _call_ai(snapshot: Snapshot, user_message: str = "") -> TradeCommand:
    if not AI_API_KEY:
        return TradeCommand(action="none", reason="AI_API_KEY missing")

    pattern = _candlestick_summary(snapshot.candles_m1)
    style = _load_json(STYLE_FILE, {"risk_preference": "conservative"})

    system_prompt = (
        "You are an execution-focused XAUUSD trading copilot. "
        "Always provide strict SL/TP and obey risk controls. "
        "If uncertain output action=none."
    )
    user_payload = {
        "mode": runtime.mode,
        "snapshot": snapshot.model_dump(),
        "pattern": pattern,
        "style": style,
        "review_notes": REVIEW_FILE.read_text(encoding="utf-8") if REVIEW_FILE.exists() else "",
        "user_message": user_message,
    }

    body = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "trade_command",
                "schema": TradeCommand.model_json_schema(),
            },
        },
    }

    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.post(f"{AI_BASE_URL}/chat/completions", headers=headers, json=body)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]

    parsed = TradeCommand.model_validate_json(content)
    return _risk_guard(snapshot, parsed)


def _persist_state() -> None:
    snapshot_dump = runtime.last_snapshot.model_dump() if runtime.last_snapshot else None
    state = {
        "last_update": datetime.now(timezone.utc).isoformat(),
        "mode": runtime.mode,
        "snapshot": snapshot_dump,
        "next_command": runtime.next_command.model_dump(),
    }
    _save_json(STATE_FILE, state)


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
    }


@app.get("/health")
async def health():
    return {"status": "ok", "mode": runtime.mode}
