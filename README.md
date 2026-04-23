# MT5-Assistant

一个用于 **MT5（XAUUSD）+ 外部 AI Agent** 的基础实现，目标是搭建“数据导出 -> AI 决策 -> 回传下单”的闭环。

## 已实现
- MT5 EA 对外导出：Tick、K线、持仓。
- Python Bridge 接收数据并接入多厂商 AI API。
- 支持下单类型：
  - taker: `buy_market`, `sell_market`
  - maker: `buy_limit`, `sell_limit`, `buy_stop`, `sell_stop`
- 权限模式：
  - `kernel`（内核权限）：Agent 自动生成并下发交易指令，EA 可直接执行。
  - `user`（用户权限）：只给建议，不允许自动下单。
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

## 3) 权限模式切换
### 查询当前模式
```bash
curl -H "X-API-Key: change_me" http://127.0.0.1:8000/v1/agent/mode
```

### 切到内核权限（自动交易）
```bash
curl -X POST http://127.0.0.1:8000/v1/agent/mode \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change_me" \
  -d '{"mode":"kernel","reason":"start autonomous execution"}'
```

### 切到用户权限（仅建议）
```bash
curl -X POST http://127.0.0.1:8000/v1/agent/mode \
  -H "Content-Type: application/json" \
  -H "X-API-Key: change_me" \
  -d '{"mode":"user","reason":"manual supervision"}'
```

## 4) AI 接入（默认 Gemini，仍支持多厂商）
Python 侧支持三类：
1. `gemini`：原生 Google Gemini API（默认）
2. `anthropic`：原生 Claude Messages API
3. `openai_compatible`：兼容 OpenAI Chat Completions 的全系网关（OpenAI、DeepSeek、Qwen兼容网关、Moonshot兼容网关等）

在 `.env` 配置：
- `AI_PROVIDER=openai_compatible|anthropic|gemini`
- `AI_BASE_URL`
- `AI_API_KEY`
- `AI_MODEL`

常用 `AI_MODEL` 示例：
- OpenAI: `gpt-4.1-mini` / `gpt-4.1` / `o4-mini`
- Claude: `claude-3-7-sonnet-latest` / `claude-3-5-sonnet-latest`
- Gemini: `gemini-2.5-pro` / `gemini-2.5-flash`
- DeepSeek(OpenAI兼容): `deepseek-chat` / `deepseek-reasoner`
- Qwen(OpenAI兼容网关): `qwen-max` / `qwen-plus`

示例（Claude 原生）：
```bash
AI_PROVIDER=anthropic
AI_BASE_URL=https://api.anthropic.com
AI_API_KEY=xxx
AI_MODEL=claude-3-7-sonnet-latest
```

默认示例（Gemini 原生）：
```bash
AI_PROVIDER=gemini
AI_BASE_URL=https://generativelanguage.googleapis.com
AI_API_KEY=xxx
AI_MODEL=gemini-2.5-flash
GEMINI_PROXY_URL=http://127.0.0.1:7897
```

Vertex AI 模式（可不填 `AI_API_KEY`）：
```bash
export GOOGLE_CLOUD_PROJECT=810669871257
export GOOGLE_CLOUD_LOCATION=global
export GOOGLE_GENAI_USE_VERTEXAI=True
```

示例（DeepSeek OpenAI兼容）：
```bash
AI_PROVIDER=openai_compatible
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=xxx
AI_MODEL=deepseek-chat
```

## 5) 关于“K线形态怎么让 AI 知道”
当前实现采用双输入：
- 原始 OHLCV 序列（`candles_m1`）
- 结构化形态特征（如 `doji` / `engulfing` / trend）

建议后续扩展到 M5/M15/H1，形成多周期共振特征。

## 6) 风险声明
- “永不爆仓”无法被任何系统绝对保证。
- 本项目通过风险规则显著降低风险，但不能承诺收益。
- 请务必先在模拟盘回测与前向验证。

## 7) 下一步建议
- 加入新闻上下文 Adapter（宏观事件对黄金影响）。
- 引入回测引擎与绩效报表。
- 增加风格学习器（根据你的历史订单动态调参）。
- 做一个 Web UI 聊天框，仅保留交易相关上下文。
