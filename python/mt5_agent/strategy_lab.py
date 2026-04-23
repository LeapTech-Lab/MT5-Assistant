from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StrategyLabResult:
    total_candidates: int
    promoted: int
    tested: int


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _momentum_bps(mids: list[float], lookback: int) -> float:
    if len(mids) <= lookback or mids[-lookback - 1] == 0:
        return 0.0
    return (mids[-1] - mids[-lookback - 1]) / mids[-lookback - 1] * 10000


def _simulate_strategy(quotes: list[dict], direction: str, entry_mom_bps: float, hold_bars: int = 10) -> dict:
    mids = [((q.get("bid", 0.0) + q.get("ask", 0.0)) / 2) for q in quotes if q.get("bid") and q.get("ask")]
    if len(mids) < hold_bars + 20:
        return {"trades": 0, "win_rate": 0.0, "avg_bps": 0.0}

    results: list[float] = []
    for i in range(20, len(mids) - hold_bars):
        prev = mids[i - 10]
        if prev == 0:
            continue
        mom = (mids[i] - prev) / prev * 10000
        if direction == "long" and mom < entry_mom_bps:
            continue
        if direction == "short" and mom > -entry_mom_bps:
            continue
        pnl_bps = (mids[i + hold_bars] - mids[i]) / mids[i] * 10000
        results.append(pnl_bps if direction == "long" else -pnl_bps)

    if not results:
        return {"trades": 0, "win_rate": 0.0, "avg_bps": 0.0}
    wins = [r for r in results if r > 0]
    return {
        "trades": len(results),
        "win_rate": round(len(wins) / len(results), 3),
        "avg_bps": round(sum(results) / len(results), 3),
    }


def run_strategy_lab(data_dir: Path, symbol: str, min_trades: int = 15) -> dict:
    quotes = [q for q in _read_jsonl(data_dir / "quote_history.jsonl") if q.get("symbol") == symbol]
    trades = [t for t in _read_jsonl(data_dir / "trade_history.jsonl") if t.get("symbol") == symbol]

    mids = [((q.get("bid", 0.0) + q.get("ask", 0.0)) / 2) for q in quotes if q.get("bid") and q.get("ask")]
    recent_mom = _momentum_bps(mids, 10) if mids else 0.0

    candidates = [
        {
            "id": f"mom_long_{symbol}",
            "type": "momentum_long",
            "params": {"entry_mom_bps": 4.0, "hold_bars": 10},
            "status": "testing",
        },
        {
            "id": f"mom_short_{symbol}",
            "type": "momentum_short",
            "params": {"entry_mom_bps": 4.0, "hold_bars": 10},
            "status": "testing",
        },
    ]

    eval_long = _simulate_strategy(quotes, "long", entry_mom_bps=4.0, hold_bars=10)
    eval_short = _simulate_strategy(quotes, "short", entry_mom_bps=4.0, hold_bars=10)

    candidates[0]["evaluation"] = eval_long
    candidates[1]["evaluation"] = eval_short

    for c in candidates:
        ev = c["evaluation"]
        if ev["trades"] >= min_trades and ev["win_rate"] >= 0.55 and ev["avg_bps"] > 0:
            c["status"] = "promoted"

    payload = {
        "symbol": symbol,
        "latest_momentum_10_bps": round(recent_mom, 3),
        "trade_count": len(trades),
        "quote_count": len(quotes),
        "candidates": candidates,
    }

    (data_dir / "strategy_candidates.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def load_strategy_candidates(data_dir: Path, symbol: str) -> dict:
    path = data_dir / "strategy_candidates.json"
    if not path.exists():
        return {"symbol": symbol, "candidates": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("symbol") != symbol:
            return {"symbol": symbol, "candidates": []}
        return data
    except json.JSONDecodeError:
        return {"symbol": symbol, "candidates": []}
