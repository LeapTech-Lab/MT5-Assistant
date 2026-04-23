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
- `AI_CALL_MIN_INTERVAL`（最小调用间隔，默认 10 秒）
- `AI_FORCE_INTERVAL`（最长强制调用间隔，默认 60 秒）
- `AI_TRIGGER_PRICE_BPS`（价格触发阈值，默认 1.5 bps）

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

> 注意：当 `GOOGLE_GENAI_USE_VERTEXAI=True` 时，建议留空 `AI_API_KEY`，避免 SDK 优先走 API key 鉴权。

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

## 6) 为什么开了 kernel 还“不下单”
常见原因：
- 命中节流：`AI_CALL_MIN_INTERVAL` 内会跳过 AI，避免高频耗 token。
- 市场变化不明显：未出现新 M1、价格波动不足（`AI_TRIGGER_PRICE_BPS`）时会跳过。
- 风险守卫拒绝：无 SL/TP、风险超阈值会被降级为 `action=none`。
- 鉴权冲突：Vertex 模式下若填了 `AI_API_KEY`，可能优先走 key 而非项目/区域凭据。

建议参数（兼顾实时与成本）：
- `AI_CALL_MIN_INTERVAL=3~5`
- `AI_FORCE_INTERVAL=20~30`
- `AI_TRIGGER_PRICE_BPS=0.8~1.2`

## 7) 风险声明
- “永不爆仓”无法被任何系统绝对保证。
- 本项目通过风险规则显著降低风险，但不能承诺收益。
- 请务必先在模拟盘回测与前向验证。

## 8) 下一步建议
- 加入新闻上下文 Adapter（宏观事件对黄金影响）。
- 引入回测引擎与绩效报表。
- 增加风格学习器（根据你的历史订单动态调参）。
- 做一个 Web UI 聊天框，仅保留交易相关上下文。

## 9) 怎么和 AI 对话了解实时行情
你可以直接调用 `/v1/chat`，它会基于**最近一次 EA 上报的实时快照**（价格、持仓、多周期K线）回答，并给出交易建议。

### 先确认 EA 正在上报数据
```bash
curl -H "X-API-Key: change_me" "http://127.0.0.1:8000/health"
```
重点看返回里的：
- `last_symbol` 是否有值
- `candles.m1/m5/m15/h1` 是否大于 0

### 对话接口示例
```bash
curl -X POST "http://127.0.0.1:8000/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "XAUUSD",
    "message": "给我当前实时行情解读：趋势、关键支撑阻力、以及是否建议开仓"
  }'
```

### 内核模式“立即下单开仓”示例
当系统是 `kernel` 模式时，你可以直接在聊天里下达：

```bash
curl -X POST "http://127.0.0.1:8000/v1/chat" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "XAUUSD",
    "message": "立即下单开仓，给出开仓原因、逻辑和胜率预测"
  }'
```

系统会做这些事：
- 立即触发一次 AI 决策（绕过最小间隔节流）
- 返回结构化指令 `command`（含 action/volume/sl/tp）
- 返回 `decision_logic`、`win_rate_estimate`、`position_management_plan`
- 若指令可执行，会自动写入 `next-command` 队列，EA 可直接拉取执行

典型返回字段：
- `answer`：自然语言建议
- `command`：结构化交易指令（action/volume/sl/tp）
- `force_trade_requested` / `queued_for_execution`：是否是“立即下单”请求，是否已入执行队列
- `decision_logic`：当前开单逻辑摘要（H1/M15 趋势、M5/M1 形态）
- `win_rate_estimate`：当前策略胜率估计（结合历史交易胜率）
- `position_management_plan`：仓位管理建议（静态/动态止盈止损）
- `multi_tf`：多周期分析摘要
- `provider`：当前 AI 提供商

### 实战提问模板（可直接复制）
- “基于当前快照，给出 XAUUSD 的 1 分钟到 1 小时多周期共振方向。”
- “现在是震荡还是趋势？如果不开仓请明确写原因。”
- “请给一个低风险方案：入场、止损、止盈、失效条件。”

> 提示：`/v1/chat` 不需要 `X-API-Key`；但如果你希望回答足够“实时”，要先保证 EA 持续向 `/v1/mt5/ingest` 推送数据。

## 10) 内核态持仓“每次扫描 + 动态管理”
已支持：在 `kernel` 模式下，只要有持仓，每次 `ingest` 都会强制让 AI 先做“仓位管理扫描”，再考虑新开仓。

AI 现在可以返回以下管理动作：
- `modify_all_sl_tp`：批量更新当前品种所有持仓的止损/止盈（动态移动止盈止损）
- `close_all`：主动全部平仓（当行情不确定或策略失效时）

EA 已支持执行以上两个动作，因此可以实现：
- 持仓盈利时动态上移止损（保本/锁盈）
- 持仓亏损且结构破坏时主动止损离场
- 行情不明时优先收缩风险，而不是盲目继续加仓
