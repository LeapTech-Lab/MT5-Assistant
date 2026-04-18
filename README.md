# MT5-Assistant

一个用于 **MT5（XAUUSD）+ 外部 AI Agent** 的基础实现，目标是搭建“数据导出 -> AI 决策 -> 回传下单”的闭环。

## 已实现
- MT5 EA 对外导出：Tick、K线、持仓。
- Python Bridge 接收数据并接入 OpenAI 兼容 API。
- 支持下单类型：
  - taker: `buy_market`, `sell_market`
  - maker: `buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`
- 风险守卫：
  - 最小手数 0.01
  - 无 SL/TP 拒绝
  - 单笔风险阈值过滤
- 复盘记忆：订单结果持续写入 `trade_review.md`
- 对话接口：`/v1/chat`

## 目录
- `mql5/Experts/AgentBridgeEA.mq5`：EA 桥接器
- `python/mt5_agent/app.py`：FastAPI 服务
- `docs/ARCHITECTURE.md`：架构说明

## 1) MT5 侧配置
1. 将 `mql5/Experts/AgentBridgeEA.mq5` 放入 MT5 `Experts` 目录并编译。
2. 在 MT5 中允许 WebRequest URL：`http://127.0.0.1:8000`
3. EA 参数里设置：
   - `InpBridgeBaseUrl`
   - `InpApiKey`
   - `InpSymbol=XAUUSD`

## 2) Python Bridge 启动
```bash
cd python
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn mt5_agent.app:app --host 0.0.0.0 --port 8000 --reload
```

## 3) AI 接入（Claude/OpenAI/其他）
该实现使用 **OpenAI 兼容 Chat Completions** 协议。你可以替换：
- `AI_BASE_URL`
- `AI_API_KEY`
- `AI_MODEL`

如果你的 Claude 网关提供 OpenAI 兼容层，可直接接入。

## 4) 关于“K线形态怎么让 AI 知道”
当前实现采用双输入：
- 原始 OHLCV 序列（`candles_m1`）
- 结构化形态特征（如 `doji` / `engulfing` / trend）

建议后续扩展到 M5/M15/H1，形成多周期共振特征。

## 5) 风险声明
- “永不爆仓”无法被任何系统绝对保证。
- 本项目通过风险规则显著降低风险，但不能承诺收益。
- 请务必先在模拟盘回测与前向验证。

## 6) 下一步建议
- 加入新闻上下文 Adapter（宏观事件对黄金影响）。
- 引入回测引擎与绩效报表。
- 增加风格学习器（根据你的历史订单动态调参）。
- 做一个 Web UI 聊天框，仅保留交易相关上下文。
