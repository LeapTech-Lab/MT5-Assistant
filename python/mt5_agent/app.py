from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types as genai_types
from pydantic import BaseModel, Field, field_validator
from .strategy_lab import load_strategy_candidates, run_strategy_lab

load_dotenv()

logger = logging.getLogger("mt5_bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
API_KEY              = os.getenv("BRIDGE_API_KEY", "change_me")
AI_PROVIDER          = os.getenv("AI_PROVIDER", "gemini").lower()
AI_BASE_URL          = os.getenv("AI_BASE_URL", "https://generativelanguage.googleapis.com")
AI_API_KEY           = os.getenv("AI_API_KEY", "")
AI_MODEL             = os.getenv("AI_MODEL", "gemini-2.5-flash")
AI_TIMEOUT_SECONDS   = int(os.getenv("AI_TIMEOUT_SECONDS", "30"))
OPENAI_PATH          = os.getenv("OPENAI_PATH", "/chat/completions")
ANTHROPIC_VERSION    = os.getenv("ANTHROPIC_VERSION", "2023-06-01")
GEMINI_PATH_TEMPLATE = os.getenv("GEMINI_PATH_TEMPLATE", "/v1beta/models/{model}:generateContent")
GEMINI_PROXY_URL     = os.getenv("GEMINI_PROXY_URL", "").strip()
MAX_RISK_PERCENT     = float(os.getenv("MAX_RISK_PERCENT", "1.0"))
DEFAULT_AGENT_MODE   = os.getenv("DEFAULT_AGENT_MODE", "user")
AI_CALL_MIN_INTERVAL = float(os.getenv("AI_CALL_MIN_INTERVAL", "10"))
AI_FORCE_INTERVAL    = float(os.getenv("AI_FORCE_INTERVAL", "60"))
AI_TRIGGER_PRICE_BPS = float(os.getenv("AI_TRIGGER_PRICE_BPS", "1.5"))
REVIEW_EVERY_N_TRADES = int(os.getenv("REVIEW_EVERY_N_TRADES", "10"))
RISK_CONTRACT_MULTIPLIER = float(os.getenv("RISK_CONTRACT_MULTIPLIER", "1.0"))
LAB_MIN_BACKTEST_TRADES = int(os.getenv("LAB_MIN_BACKTEST_TRADES", "15"))

# 历史交易最多回传给AI的条数
TRADE_HISTORY_FOR_AI = int(os.getenv("TRADE_HISTORY_FOR_AI", "20"))

DATA_DIR     = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE   = DATA_DIR / "state.json"
STYLE_FILE   = DATA_DIR / "style_profile.json"
STRATEGY_FILE = DATA_DIR / "strategy_playbook.json"
# 结构化交易历史（JSONL，每行一条）
TRADE_LOG    = DATA_DIR / "trade_history.jsonl"
QUOTE_LOG    = DATA_DIR / "quote_history.jsonl"
# 旧的 review 文件保留兼容
REVIEW_FILE  = DATA_DIR / "trade_review.md"

_last_ai_call_time: float = 0.0

app = FastAPI(title="MT5 AI Bridge v2")


# ─────────────────────────────────────────────────────────────
# 422 详情日志
# ─────────────────────────────────────────────────────────────
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    body = await request.body()
    logger.error("422 on %s %s | errors=%s | body=%s",
                 request.method, request.url.path,
                 exc.errors(), body[:300].decode("utf-8", errors="replace"))
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


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
    model_config = {"extra": "ignore"}


class Candle(BaseModel):
    """支持两种字段名：完整名（time/open/...）和短名（t/o/h/l/c/v）"""
    # 完整字段名（兼容旧版EA）
    time:  Optional[str]   = None
    open:  Optional[float] = None
    high:  Optional[float] = None
    low:   Optional[float] = None
    close: Optional[float] = None
    tick_volume: float = 0.0
    real_volume: Optional[float] = None
    # 短字段名（新版EA）
    t: Optional[str]   = None
    o: Optional[float] = None
    h: Optional[float] = None
    l: Optional[float] = None
    c: Optional[float] = None
    v: Optional[float] = None

    model_config = {"extra": "ignore"}

    @field_validator("tick_volume", mode="before")
    @classmethod
    def coerce_tick_volume(cls, v):
        try:
            return max(float(v), 0.0)
        except (TypeError, ValueError):
            return 0.0

    # 统一取值属性
    @property
    def ts(self) -> str:
        return self.time or self.t or ""

    @property
    def o_(self) -> float:
        return self.open if self.open is not None else (self.o or 0.0)

    @property
    def h_(self) -> float:
        return self.high if self.high is not None else (self.h or 0.0)

    @property
    def l_(self) -> float:
        return self.low if self.low is not None else (self.l or 0.0)

    @property
    def c_(self) -> float:
        return self.close if self.close is not None else (self.c or 0.0)

    @property
    def vol(self) -> float:
        return self.tick_volume or self.v or 0.0

    def to_compact(self) -> dict:
        """返回给AI的精简字典"""
        return {"t": self.ts, "o": self.o_, "h": self.h_, "l": self.l_, "c": self.c_, "v": self.vol}


class Snapshot(BaseModel):
    symbol: str
    bid: float
    ask: float
    time: str
    positions: list[Position] = Field(default_factory=list)
    candles_m1:  list[Candle] = Field(default_factory=list)
    candles_m5:  list[Candle] = Field(default_factory=list)
    candles_m15: list[Candle] = Field(default_factory=list)
    candles_h1:  list[Candle] = Field(default_factory=list)
    model_config = {"extra": "ignore"}

    @field_validator("candles_m1", "candles_m5", "candles_m15", "candles_h1", mode="before")
    @classmethod
    def ensure_list(cls, v):
        return v if isinstance(v, list) else []


class TradeCommand(BaseModel):
    action: Literal[
        "none", "buy_market", "sell_market",
        "buy_limit", "sell_limit", "buy_stop", "sell_stop",
        "close_all", "modify_all_sl_tp",
    ] = "none"
    volume: float = 0.01
    sl: float = 0.0
    tp: float = 0.0
    price: float = 0.0
    reason: str = ""


class TradeRecord(BaseModel):
    """结构化交易记录，存入 trade_history.jsonl"""
    ts:         str
    symbol:     str
    action:     str
    volume:     float
    exec_price: float
    sl:         float
    tp:         float
    ticket:     int
    ok:         bool
    retcode:    int
    comment:    str
    decision_reason: Optional[str] = None
    # 平仓后由外部更新（可选）
    close_price: Optional[float] = None
    profit:      Optional[float] = None
    outcome:     Optional[str]   = None   # "win" / "loss" / "breakeven"


class ChatReq(BaseModel):
    message: str
    symbol: str = "BTCUSD"


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


def _append_trade_record(record: TradeRecord) -> None:
    """追加一条结构化交易记录到 JSONL 文件"""
    with TRADE_LOG.open("a", encoding="utf-8") as f:
        f.write(record.model_dump_json() + "\n")


def _append_quote_snapshot(snapshot: Snapshot) -> None:
    quote = {
        "ts": snapshot.time,
        "symbol": snapshot.symbol,
        "bid": snapshot.bid,
        "ask": snapshot.ask,
        "spread": round(max(snapshot.ask - snapshot.bid, 0.0), 8),
        "positions": len(snapshot.positions),
    }
    with QUOTE_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(quote, ensure_ascii=False) + "\n")


def _load_recent_quotes(symbol: str, n: int = 300) -> list[dict]:
    if not QUOTE_LOG.exists():
        return []
    lines = QUOTE_LOG.read_text(encoding="utf-8").strip().splitlines()
    recent = lines[-max(n * 3, n):] if len(lines) > n else lines
    records: list[dict] = []
    for line in recent:
        try:
            q = json.loads(line)
            if q.get("symbol") == symbol:
                records.append(q)
        except json.JSONDecodeError:
            pass
    return records[-n:]


def _quote_cache_features(snapshot: Snapshot, n: int = 240) -> dict:
    quotes = _load_recent_quotes(snapshot.symbol, n)
    if not quotes:
        return {"count": 0}
    mids = [((q.get("bid", 0) + q.get("ask", 0)) / 2) for q in quotes if q.get("bid") and q.get("ask")]
    spreads = [q.get("spread", 0) for q in quotes]
    if not mids:
        return {"count": len(quotes)}
    latest_mid = mids[-1]
    prev_mid = mids[0]
    drift_bps = ((latest_mid - prev_mid) / prev_mid * 10000) if prev_mid else 0.0
    mom_10 = 0.0
    if len(mids) > 10 and mids[-11] != 0:
        mom_10 = (mids[-1] - mids[-11]) / mids[-11] * 10000
    return {
        "count": len(quotes),
        "drift_bps": round(drift_bps, 2),
        "momentum_10_bps": round(mom_10, 2),
        "spread_avg": round(sum(spreads) / len(spreads), 8) if spreads else 0.0,
        "spread_latest": spreads[-1] if spreads else 0.0,
        "mid_latest": latest_mid,
    }


def _load_strategy_playbook() -> dict:
    default = {
        "version": 1,
        "last_review_trade_count": 0,
        "parameters": {
            "sl_buffer_factor": 1.0,
            "tp_factor": 1.5,
            "risk_mode": "conservative",
        },
        "rules": [],
        "notes": [],
    }
    return _load_json(STRATEGY_FILE, default)


def _save_strategy_playbook(playbook: dict) -> None:
    _save_json(STRATEGY_FILE, playbook)


def _load_recent_trades(n: int = TRADE_HISTORY_FOR_AI) -> list[dict]:
    """读取最近 n 条交易记录（从文件尾部）"""
    if not TRADE_LOG.exists():
        return []
    lines = TRADE_LOG.read_text(encoding="utf-8").strip().splitlines()
    recent = lines[-n:] if len(lines) > n else lines
    records = []
    for line in recent:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def _trade_summary(trades: list[dict]) -> dict:
    """生成交易统计摘要给AI参考"""
    if not trades:
        return {"total": 0}
    wins   = [t for t in trades if t.get("outcome") == "win"]
    losses = [t for t in trades if t.get("outcome") == "loss"]
    profits = [t["profit"] for t in trades if t.get("profit") is not None]
    return {
        "total":      len(trades),
        "wins":       len(wins),
        "losses":     len(losses),
        "win_rate":   round(len(wins) / len(trades), 2) if trades else 0,
        "total_pnl":  round(sum(profits), 2) if profits else None,
        "avg_pnl":    round(sum(profits) / len(profits), 2) if profits else None,
        "last_3":     trades[-3:],   # 最近3笔详情
    }


def _extract_reason_tag(reason: str | None) -> str:
    text = (reason or "").lower()
    if "trend" in text:
        return "trend_follow"
    if "breakout" in text:
        return "breakout"
    if "reversal" in text:
        return "reversal"
    if "mean" in text:
        return "mean_reversion"
    return "unspecified"


def _review_and_update_strategy() -> dict:
    trades = _load_recent_trades(500)
    playbook = _load_strategy_playbook()
    total = len(trades)
    if total == 0:
        return {"reviewed": False, "reason": "no trades"}
    if total - int(playbook.get("last_review_trade_count", 0)) < REVIEW_EVERY_N_TRADES:
        return {"reviewed": False, "reason": "review interval not reached", "total": total}

    closed = [t for t in trades if t.get("outcome") in {"win", "loss"}]
    wins = [t for t in closed if t.get("outcome") == "win"]
    losses = [t for t in closed if t.get("outcome") == "loss"]
    by_action: dict[str, dict[str, int]] = {}
    by_reason: dict[str, dict[str, int]] = {}
    for t in closed:
        action = str(t.get("action", "unknown"))
        reason_tag = _extract_reason_tag(t.get("decision_reason"))
        by_action.setdefault(action, {"win": 0, "loss": 0})
        by_reason.setdefault(reason_tag, {"win": 0, "loss": 0})
        if t.get("outcome") == "win":
            by_action[action]["win"] += 1
            by_reason[reason_tag]["win"] += 1
        else:
            by_action[action]["loss"] += 1
            by_reason[reason_tag]["loss"] += 1

    params = playbook.get("parameters", {})
    sl_buffer = float(params.get("sl_buffer_factor", 1.0))
    tp_factor = float(params.get("tp_factor", 1.5))
    risk_mode = str(params.get("risk_mode", "conservative"))
    new_rules = []

    buy_losses = by_action.get("buy_market", {}).get("loss", 0) + by_action.get("buy_limit", {}).get("loss", 0)
    buy_wins = by_action.get("buy_market", {}).get("win", 0) + by_action.get("buy_limit", {}).get("win", 0)
    if buy_losses >= 3 and buy_losses > buy_wins:
        sl_buffer = min(2.0, round(sl_buffer + 0.1, 2))
        risk_mode = "defensive"
        new_rules.append("做多近期止损偏多：增大SL缓冲（sl_buffer_factor +0.1），并降低追涨频率。")

    trend_stats = by_reason.get("trend_follow", {"win": 0, "loss": 0})
    if trend_stats["loss"] >= 3 and trend_stats["loss"] > trend_stats["win"]:
        tp_factor = max(1.0, round(tp_factor - 0.1, 2))
        new_rules.append("趋势跟随胜率下降：缩短止盈目标（tp_factor -0.1），优先保本。")

    if len(losses) >= 2 and len(wins) == 0:
        risk_mode = "minimal"
        new_rules.append("连续亏损期：仅在H1与M15同向且M5确认时开仓，其他时段以管理仓位为主。")

    playbook["parameters"] = {
        "sl_buffer_factor": sl_buffer,
        "tp_factor": tp_factor,
        "risk_mode": risk_mode,
    }
    existing_rules = playbook.get("rules", [])
    playbook["rules"] = (existing_rules + new_rules)[-50:]
    note = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "total_closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "by_action": by_action,
        "by_reason": by_reason,
        "new_rules_count": len(new_rules),
    }
    playbook["notes"] = (playbook.get("notes", []) + [note])[-100:]
    playbook["last_review_trade_count"] = total
    _save_strategy_playbook(playbook)
    return {"reviewed": True, "new_rules_count": len(new_rules), "parameters": playbook["parameters"]}


# ─────────────────────────────────────────────────────────────
# 多周期K线分析
# ─────────────────────────────────────────────────────────────

def _candle_pattern(candles: list[Candle], label: str) -> dict:
    """识别单周期的形态和趋势"""
    if len(candles) < 3:
        return {"tf": label, "pattern": "unknown", "trend": "unknown"}

    c1, c2, c3 = candles[-1], candles[-2], candles[-3]
    trend = "up" if c1.c_ > c3.c_ else "down"
    body  = abs(c1.c_ - c1.o_)
    wick  = c1.h_ - c1.l_
    ratio = body / wick if wick > 0 else 0

    if ratio < 0.2:
        pattern = "doji"
    elif c1.c_ > c1.o_ and c2.c_ < c2.o_ and c1.c_ > c2.o_:
        pattern = "bullish_engulfing"
    elif c1.c_ < c1.o_ and c2.c_ > c2.o_ and c1.c_ < c2.o_:
        pattern = "bearish_engulfing"
    elif c1.c_ > c1.o_ and (c1.h_ - c1.c_) > 2 * body:
        pattern = "shooting_star"
    elif c1.c_ < c1.o_ and (c1.c_ - c1.l_) > 2 * body:
        pattern = "hammer"
    else:
        pattern = "normal"

    # 简单均线趋势（用最后10根收盘价）
    closes = [c.c_ for c in candles[-10:]]
    ma = sum(closes) / len(closes) if closes else 0

    return {
        "tf":          label,
        "pattern":     pattern,
        "trend":       trend,
        "last_close":  c1.c_,
        "ma10":        round(ma, 2),
        "above_ma10":  c1.c_ > ma,
    }


def _multi_tf_analysis(snapshot: Snapshot) -> dict:
    """汇总多周期分析结果"""
    return {
        "m1":  _candle_pattern(snapshot.candles_m1,  "M1"),
        "m5":  _candle_pattern(snapshot.candles_m5,  "M5"),
        "m15": _candle_pattern(snapshot.candles_m15, "M15"),
        "h1":  _candle_pattern(snapshot.candles_h1,  "H1"),
    }


def _risk_guard(snapshot: Snapshot, cmd: TradeCommand) -> TradeCommand:
    if cmd.action == "none":
        return cmd
    if cmd.action == "close_all":
        return cmd
    if cmd.action == "modify_all_sl_tp":
        if cmd.sl <= 0 or cmd.tp <= 0:
            return TradeCommand(action="none", reason="modify_all_sl_tp requires SL/TP")
        if not snapshot.positions:
            return TradeCommand(action="none", reason="No positions to modify")
        return cmd
    if cmd.volume < 0.01:
        cmd.volume = 0.01
    if cmd.sl <= 0 or cmd.tp <= 0:
        return TradeCommand(action="none", reason="Missing SL/TP rejected by risk guard")
    account_equity = 10000.0
    max_loss = account_equity * (MAX_RISK_PERCENT / 100)
    ref_price = cmd.price if cmd.price > 0 else snapshot.bid
    distance = abs(ref_price - cmd.sl)
    risk_value = distance * cmd.volume * RISK_CONTRACT_MULTIPLIER
    if risk_value > max_loss:
        # 自动缩小手数，尽量不直接拒单
        if distance > 0:
            allowed_volume = max_loss / (distance * RISK_CONTRACT_MULTIPLIER)
            if allowed_volume >= 0.01:
                cmd.volume = round(allowed_volume, 2)
                cmd.reason = f"{cmd.reason} | volume auto-adjusted by risk guard".strip(" |")
                return cmd
        return TradeCommand(action="none", reason="Risk too large")
    return cmd


def _extract_first_json_block(text: str) -> str:
    start = text.find("{")
    end   = text.rfind("}")
    if start < 0 or end <= start:
        return ""
    return text[start: end + 1]


def _normalize_trade_command(raw_text: str) -> TradeCommand:
    cleaned = _extract_first_json_block(raw_text)
    if not cleaned:
        return TradeCommand(action="none", reason="Model output has no JSON command")
    try:
        return TradeCommand.model_validate_json(cleaned)
    except Exception:
        return TradeCommand(action="none", reason="Invalid command JSON from model")


def _build_ai_payload(snapshot: Snapshot, user_message: str = "") -> dict:
    recent_trades = _load_recent_trades(TRADE_HISTORY_FOR_AI)
    trade_stats   = _trade_summary(recent_trades)
    mtf           = _multi_tf_analysis(snapshot)
    style         = _load_json(STYLE_FILE, {"risk_preference": "conservative"})
    strategy      = _load_strategy_playbook()
    strategy_candidates = load_strategy_candidates(DATA_DIR, snapshot.symbol)
    quote_features = _quote_cache_features(snapshot)

    return {
        "mode":     runtime.mode,
        "symbol":   snapshot.symbol,
        "price":    {"bid": snapshot.bid, "ask": snapshot.ask, "time": snapshot.time},
        "positions": [p.model_dump() for p in snapshot.positions],

        # ── 多周期K线（精简字段节省token）──
        "candles": {
            "m1":  [c.to_compact() for c in snapshot.candles_m1],
            "m5":  [c.to_compact() for c in snapshot.candles_m5],
            "m15": [c.to_compact() for c in snapshot.candles_m15],
            "h1":  [c.to_compact() for c in snapshot.candles_h1],
        },

        # ── 多周期形态分析 ──
        "multi_tf_analysis": mtf,

        # ── 历史交易回顾 ──
        "trade_history": {
            "summary": trade_stats,
            "recent":  recent_trades,
        },

        "style":        style,
        "strategy_playbook": strategy,
        "strategy_candidates": strategy_candidates,
        "quote_cache_features": quote_features,
        "user_message": user_message,

        "instructions": (
            "You are a professional crypto trading AI for {symbol}. "
            "Analyze the multi-timeframe data: use H1 for trend direction, "
            "M15 for structure, M5 for entry timing, M1 for precise entry. "
            "Additionally, use quote_cache_features from local quote cache for momentum/spread regime confirmation. "
            "Use strategy_candidates as candidate brains: treat promoted candidates as high-priority templates, "
            "and keep testing non-promoted candidates with small risk. "
            "On EVERY call, you must scan all existing positions first. "
            "If positions are open, prioritize position management: "
            "you may return modify_all_sl_tp to dynamically adjust stop-loss/take-profit, "
            "or close_all when risk becomes unclear or market invalidates the thesis. "
            "Review the trade history to learn from past wins and losses. "
            "Only trade when H1 and M15 agree on direction. "
            "Always provide SL and TP. Reply ONLY with a JSON object matching the schema."
        ).format(symbol=snapshot.symbol),

        "required_json_schema": TradeCommand.model_json_schema(),
    }


def _is_force_trade_request(message: str) -> bool:
    text = (message or "").lower()
    keywords = [
        "立即下单", "立即开仓", "马上开仓", "马上下单", "当前必须下单", "必须下单",
        "market now", "open now", "buy now", "sell now",
    ]
    return any(k in text for k in keywords)


def _force_trade_fallback(snapshot: Snapshot) -> TradeCommand:
    """强制下单场景的兜底指令：极小仓位 + 保守SL/TP，降低被拒概率。"""
    mtf = _multi_tf_analysis(snapshot)
    up_votes = sum(1 for tf in ("m1", "m5", "m15", "h1") if mtf[tf].get("trend") == "up")
    down_votes = sum(1 for tf in ("m1", "m5", "m15", "h1") if mtf[tf].get("trend") == "down")
    is_buy = up_votes >= down_votes

    entry = snapshot.ask if is_buy else snapshot.bid
    spread = max(snapshot.ask - snapshot.bid, entry * 0.0001)
    sl_distance = max(entry * 0.0012, spread * 20)   # ~0.12%
    tp_distance = sl_distance * 1.2
    sl = entry - sl_distance if is_buy else entry + sl_distance
    tp = entry + tp_distance if is_buy else entry - tp_distance

    return TradeCommand(
        action="buy_market" if is_buy else "sell_market",
        volume=0.01,
        price=entry,
        sl=sl,
        tp=tp,
        reason="force-trade fallback: minimal-risk execution",
    )


def _build_trade_explanation(snapshot: Snapshot, cmd: TradeCommand) -> dict:
    mtf = _multi_tf_analysis(snapshot)
    trades = _load_recent_trades(TRADE_HISTORY_FOR_AI)
    stats = _trade_summary(trades)

    h1_trend = mtf["h1"].get("trend", "unknown")
    m15_trend = mtf["m15"].get("trend", "unknown")
    trend_aligned = h1_trend == m15_trend and h1_trend in {"up", "down"}

    historical_win_rate = float(stats.get("win_rate", 0) or 0)
    base_rate = 0.5 if trend_aligned else 0.4
    if cmd.action in {"buy_market", "sell_market", "buy_limit", "sell_limit", "buy_stop", "sell_stop"}:
        base_rate += 0.05
    if cmd.action == "none":
        base_rate -= 0.1
    estimated = max(0.2, min(0.85, (base_rate * 0.6 + historical_win_rate * 0.4)))

    return {
        "decision_logic": {
            "h1_trend": h1_trend,
            "m15_trend": m15_trend,
            "trend_aligned": trend_aligned,
            "m5_pattern": mtf["m5"].get("pattern", "unknown"),
            "m1_pattern": mtf["m1"].get("pattern", "unknown"),
        },
        "win_rate_estimate": round(estimated, 2),
        "historical_win_rate": historical_win_rate,
        "position_management_plan": {
            "type": "dynamic" if trend_aligned else "static",
            "guideline": (
                "若浮盈达到1R可上移止损到保本；趋势延续时按M5上一根K线低/高点跟踪止损。"
                if trend_aligned else
                "固定止损止盈，达到TP前不加仓，连续两笔亏损后暂停。"
            ),
        },
    }


def _to_bool(v: str | None) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _should_call_ai(current: Snapshot, previous: Snapshot | None) -> tuple[bool, str]:
    """在节省 token 与实时性之间做平衡的触发器。"""
    now = time.time()
    elapsed = now - _last_ai_call_time

    if previous is None:
        return True, "first snapshot"
    if elapsed >= AI_FORCE_INTERVAL:
        return True, f"force interval reached ({elapsed:.1f}s)"
    if elapsed < AI_CALL_MIN_INTERVAL:
        return False, f"min interval guard ({elapsed:.1f}s<{AI_CALL_MIN_INTERVAL:.1f}s)"

    prev_ts = previous.candles_m1[-1].ts if previous.candles_m1 else ""
    curr_ts = current.candles_m1[-1].ts if current.candles_m1 else ""
    if curr_ts and curr_ts != prev_ts:
        return True, "new M1 candle"

    prev_mid = (previous.bid + previous.ask) / 2 if (previous.bid > 0 and previous.ask > 0) else 0.0
    curr_mid = (current.bid + current.ask) / 2 if (current.bid > 0 and current.ask > 0) else 0.0
    if prev_mid > 0 and curr_mid > 0:
        move_bps = abs(curr_mid - prev_mid) / prev_mid * 10000
        if move_bps >= AI_TRIGGER_PRICE_BPS:
            return True, f"price move {move_bps:.2f} bps"

    if len(current.positions) != len(previous.positions):
        return True, "position count changed"

    return False, "no meaningful market change"


# ─────────────────────────────────────────────────────────────
# AI Provider Calls
# ─────────────────────────────────────────────────────────────

async def _call_openai_compatible(client: httpx.AsyncClient, prompt_json: str) -> TradeCommand:
    body = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": "You are a professional trading AI. Reply only in JSON."},
            {"role": "user",   "content": prompt_json},
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
        "max_tokens": 800,
        "system": "You are a professional trading AI. Reply only in JSON.",
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
    text = "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    return _normalize_trade_command(text)


def _call_gemini_sync(prompt_json: str) -> TradeCommand:
    http_options = None
    if GEMINI_PROXY_URL:
        http_options = genai_types.HttpOptions(
            client_args={"transport": httpx.HTTPTransport(proxy=GEMINI_PROXY_URL)},
            async_client_args={"transport": httpx.AsyncHTTPTransport(proxy=GEMINI_PROXY_URL)},
        )

    use_vertex = _to_bool(os.getenv("GOOGLE_GENAI_USE_VERTEXAI"))
    if use_vertex:
        client = genai.Client(http_options=http_options)
    else:
        client = genai.Client(api_key=AI_API_KEY, http_options=http_options)
    response = client.models.generate_content(
        model=AI_MODEL,
        contents=prompt_json,
        config=genai_types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return _normalize_trade_command(response.text or "")


async def _call_gemini(_client: httpx.AsyncClient, prompt_json: str) -> TradeCommand:
    return await asyncio.to_thread(_call_gemini_sync, prompt_json)


async def _call_ai(snapshot: Snapshot, user_message: str = "", force_call: bool = False) -> TradeCommand:
    global _last_ai_call_time

    if AI_PROVIDER in {"openai", "openai_compatible", "deepseek", "moonshot", "qwen", "siliconflow", "anthropic"} and not AI_API_KEY:
        return TradeCommand(action="none", reason="AI_API_KEY missing")

    now     = time.time()
    elapsed = now - _last_ai_call_time
    if (not force_call) and elapsed < AI_CALL_MIN_INTERVAL:
        wait = AI_CALL_MIN_INTERVAL - elapsed
        logger.info("Rate limit guard: skip AI, next in %.1fs", wait)
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
            _last_ai_call_time = time.time() + 30
            logger.warning("429 Too Many Requests, backing off 30s")
            return TradeCommand(action="none", reason="API rate limited, backing off")
        logger.error("AI HTTP error %s", e.response.status_code)
        return TradeCommand(action="none", reason=f"AI HTTP error {e.response.status_code}")
    except Exception as e:
        logger.error("AI call failed: %s", e)
        return TradeCommand(action="none", reason=f"AI call exception: {type(e).__name__}")

    return _risk_guard(snapshot, cmd)


def _persist_state() -> None:
    state = {
        "last_update":  datetime.now(timezone.utc).isoformat(),
        "mode":         runtime.mode,
        "next_command": runtime.next_command.model_dump(),
        "last_symbol":  runtime.last_snapshot.symbol if runtime.last_snapshot else None,
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
    _persist_state()
    return {"ok": True, "mode": runtime.mode}


@app.get("/v1/agent/mode")
async def get_mode(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return {"mode": runtime.mode}


@app.post("/v1/mt5/ingest")
async def ingest(snapshot: Snapshot, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    previous_snapshot = runtime.last_snapshot
    runtime.last_snapshot = snapshot
    _append_quote_snapshot(snapshot)

    logger.info(
        "Ingest | %s bid=%.2f ask=%.2f M1=%d M5=%d M15=%d H1=%d pos=%d",
        snapshot.symbol, snapshot.bid, snapshot.ask,
        len(snapshot.candles_m1), len(snapshot.candles_m5),
        len(snapshot.candles_m15), len(snapshot.candles_h1),
        len(snapshot.positions),
    )

    if runtime.mode == "kernel":
        # 有持仓时，每次都强制让AI做一次仓位扫描与管理决策
        if snapshot.positions:
            should_call, reason = True, "position management pass"
        else:
            should_call, reason = _should_call_ai(snapshot, previous_snapshot)
        if should_call:
            cmd = await _call_ai(
                snapshot,
                user_message="Kernel ingest pass: scan current positions and manage them dynamically before new entries.",
                force_call=bool(snapshot.positions),
            )
            runtime.next_command = cmd
        else:
            cmd = TradeCommand(action="none", reason=f"AI skipped: {reason}")
    else:
        cmd = TradeCommand(action="none", reason="User mode: no auto-trading")
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

    snap = runtime.last_snapshot
    record = TradeRecord(
        ts          = datetime.now(timezone.utc).isoformat(),
        symbol      = snap.symbol if snap else "UNKNOWN",
        action      = payload.get("action", ""),
        volume      = float(payload.get("volume", 0)),
        exec_price  = float(payload.get("exec_price", 0)),
        sl          = float(payload.get("sl", 0)),
        tp          = float(payload.get("tp", 0)),
        ticket      = int(payload.get("ticket", 0)),
        ok          = bool(payload.get("ok", False)),
        retcode     = int(payload.get("retcode", 0)),
        comment     = str(payload.get("comment", "")),
        decision_reason = payload.get("reason"),
    )
    _append_trade_record(record)
    review = _review_and_update_strategy()
    lab = run_strategy_lab(DATA_DIR, record.symbol, min_trades=LAB_MIN_BACKTEST_TRADES)
    logger.info("Trade recorded | action=%s ok=%s ticket=%d price=%.2f",
                record.action, record.ok, record.ticket, record.exec_price)
    return {"ok": True, "strategy_review": review, "strategy_lab": lab}


@app.post("/v1/mt5/close-result")
async def close_result(payload: dict, x_api_key: str | None = Header(default=None)):
    """
    可选：平仓时从EA调用此接口更新最近一条记录的盈亏。
    payload: { "ticket": 123, "close_price": 77500.0, "profit": 23.5 }
    """
    _auth(x_api_key)
    if not TRADE_LOG.exists():
        return {"ok": False, "reason": "No trade log"}

    ticket      = int(payload.get("ticket", 0))
    close_price = float(payload.get("close_price", 0))
    profit      = float(payload.get("profit", 0))
    outcome     = "win" if profit > 0 else ("loss" if profit < 0 else "breakeven")

    lines = TRADE_LOG.read_text(encoding="utf-8").splitlines()
    updated = False
    new_lines = []
    for line in reversed(lines):
        if not updated:
            try:
                rec = json.loads(line)
                if rec.get("ticket") == ticket:
                    rec["close_price"] = close_price
                    rec["profit"]      = profit
                    rec["outcome"]     = outcome
                    line = json.dumps(rec, ensure_ascii=False)
                    updated = True
            except Exception:
                pass
        new_lines.append(line)
    TRADE_LOG.write_text("\n".join(reversed(new_lines)) + "\n", encoding="utf-8")
    logger.info("Close result updated | ticket=%d profit=%.2f outcome=%s", ticket, profit, outcome)
    return {"ok": updated, "outcome": outcome}


@app.get("/v1/trade-history")
async def get_trade_history(n: int = 50, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    trades = _load_recent_trades(n)
    return {"total": len(trades), "summary": _trade_summary(trades), "trades": trades}


@app.get("/v1/quotes/recent")
async def get_recent_quotes(symbol: str, n: int = 200, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    quotes = _load_recent_quotes(symbol=symbol, n=n)
    features = _quote_cache_features(Snapshot(
        symbol=symbol,
        bid=quotes[-1]["bid"] if quotes else 0.0,
        ask=quotes[-1]["ask"] if quotes else 0.0,
        time=quotes[-1]["ts"] if quotes else "",
        positions=[],
        candles_m1=[],
        candles_m5=[],
        candles_m15=[],
        candles_h1=[],
    ))
    return {"total": len(quotes), "features": features, "quotes": quotes}


@app.get("/v1/strategy/playbook")
async def get_strategy_playbook(x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return _load_strategy_playbook()


@app.get("/v1/strategy/candidates")
async def get_strategy_candidates(symbol: str, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return load_strategy_candidates(DATA_DIR, symbol)


@app.post("/v1/strategy/lab/run")
async def run_strategy_candidates(symbol: str, x_api_key: str | None = Header(default=None)):
    _auth(x_api_key)
    return run_strategy_lab(DATA_DIR, symbol, min_trades=LAB_MIN_BACKTEST_TRADES)


@app.post("/v1/chat")
async def chat(req: ChatReq):
    snapshot = runtime.last_snapshot
    if snapshot is None:
        return {"answer": "尚未收到 MT5 数据，请先启动 EA。"}
    force_trade = runtime.mode == "kernel" and _is_force_trade_request(req.message)
    cmd = await _call_ai(snapshot, req.message, force_call=force_trade)
    if force_trade and cmd.action == "none":
        cmd = _risk_guard(snapshot, _force_trade_fallback(snapshot))
    mtf = _multi_tf_analysis(snapshot)
    explain = _build_trade_explanation(snapshot, cmd)

    if force_trade and cmd.action != "none":
        runtime.next_command = cmd
        _persist_state()

    return {
        "mode":     runtime.mode,
        "answer":   f"建议动作: {cmd.action}, 手数: {cmd.volume}, SL: {cmd.sl}, TP: {cmd.tp}\n原因: {cmd.reason}",
        "command":  cmd,
        "force_trade_requested": force_trade,
        "queued_for_execution": force_trade and cmd.action != "none",
        "decision_logic": explain["decision_logic"],
        "win_rate_estimate": explain["win_rate_estimate"],
        "historical_win_rate": explain["historical_win_rate"],
        "position_management_plan": explain["position_management_plan"],
        "multi_tf": mtf,
        "provider": AI_PROVIDER,
    }


@app.get("/health")
async def health():
    snap   = runtime.last_snapshot
    trades = _load_recent_trades(5)
    return {
        "status":       "ok",
        "mode":         runtime.mode,
        "provider":     AI_PROVIDER,
        "last_symbol":  snap.symbol if snap else None,
        "candles":      {
            "m1":  len(snap.candles_m1)  if snap else 0,
            "m5":  len(snap.candles_m5)  if snap else 0,
            "m15": len(snap.candles_m15) if snap else 0,
            "h1":  len(snap.candles_h1)  if snap else 0,
        },
        "recent_trades": len(trades),
    }
