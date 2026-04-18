# MT5 + AI Agent 架构设计（XAUUSD）

## 目标
- MT5 EA 实时导出行情、K线、持仓数据。
- Python Bridge 统一接收数据并调度 AI。
- 支持市价单（taker）与挂单（maker）。
- 强制风险守卫（止损止盈、最小手数、风险上限）。
- 具备复盘记忆与交易风格学习入口。
- 支持双权限模式：内核权限（自动下单）与用户权限（仅建议）。
- 支持多厂商 AI API（OpenAI 兼容、Anthropic、Gemini）。

## 数据流
1. `AgentBridgeEA.mq5` 每秒采集：
   - Tick（bid/ask）
   - M1 K线（默认 120 根，可扩）
   - 当前持仓
2. EA `POST /v1/mt5/ingest`
3. Python 侧：
   - 计算 K 线形态特征（doji / engulfing / trend）
   - 拼接 Prompt（含仓位、形态、复盘记录、风格偏好）
   - 按 `AI_PROVIDER` 调用对应模型 API
   - 通过风险引擎过滤
4. EA `GET /v1/mt5/next-command` 拉取可执行命令并下单（仅在 kernel 模式）。

## 双权限模式
- `kernel`（内核权限）
  - `ingest` 会实时调用 AI 生成交易指令。
  - `next-command` 会返回可执行命令给 EA。
  - 用于持续自动运营与仓位维护。
- `user`（用户权限）
  - `ingest` 不输出可执行命令。
  - `next-command` 永远返回 `action=none`。
  - 用户通过 `/v1/chat` 获取实时建议，人工决策执行。

## 多厂商 API 适配
- `openai_compatible`：适配所有 OpenAI Chat Completions 兼容网关。
- `anthropic`：使用 `POST /v1/messages`。
- `gemini`：使用 `models/{model}:generateContent`。

统一输出规范：
- 模型必须返回 `TradeCommand` JSON。
- Bridge 会做 JSON 提取与结构校验，不合规则降级为 `action=none`。

## 模式控制接口
- `GET /v1/agent/mode`：查询当前模式。
- `POST /v1/agent/mode`：切换模式（写入审计日志）。

## K线形态如何传给 AI
推荐“双通道”：
- 原始通道：直接传最近 N 根 OHLCV（避免丢信息）
- 特征通道：在 Python 端额外计算形态标签、趋势、波动率等，作为低维 summary

这样可减少模型误判，也方便后续迁移为规则+模型混合策略。

## 风险与生存规则
- 每单必须有 SL / TP；否则拒绝执行。
- 单笔风险上限（示例 1% 权益）。
- 体量下限 0.01 手；可配置上限。
- 建议追加：
  - 日内最大亏损阈值
  - 连续亏损熔断
  - 新闻高波动窗口禁开仓

## 学习与记忆
- `trade_review.md`：记录每笔交易结果与复盘结论。
- `style_profile.json`：存交易风格偏好（激进/保守、偏好回调/突破等）。
- 每次 AI 决策都读入两者，形成“策略记忆”。

## 对话框
提供 `/v1/chat` 接口给前端或命令行聊天工具调用，实现“只讨论 XAUUSD 与下单逻辑”的交易助手。

## 国际新闻因素
建议新增一个 News Adapter（如 RSS/财经 API）写入 `macro_context.json`，并在 Prompt 中加入：
- 事件时间
- 币种/黄金相关性
- 影响方向与置信度

> 注意：新闻驱动信号必须降低仓位并扩大保护性止损过滤。

## Openclaw 风格落地建议
先做最小可用闭环（MVP）：
- 数据桥 + 下单桥 + AI 决策 + 风险守卫 + 复盘日志

再迭代：
- 多周期特征（M1/M5/M15/H1）
- 回测评估
- 策略学习器（离线）
