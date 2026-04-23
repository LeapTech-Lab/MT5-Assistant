#property strict
#property version   "2.00"
#property description "Bridge MT5 data to external AI agent via HTTP - Multi-Timeframe"

input string InpBridgeBaseUrl = "http://127.0.0.1:8000";
input string InpApiKey        = "change_me";
input string InpSymbol        = "BTCUSD";
input int    InpTimerSeconds  = 10;   // AI限速：10秒一次
input int    InpBarsM1        = 60;   // M1: 最近60根
input int    InpBarsM5        = 50;   // M5: 最近50根 ≈ 4小时
input int    InpBarsM15       = 50;   // M15: 最近50根 ≈ 12小时
input int    InpBarsH1        = 48;   // H1: 最近48根 = 2天

string g_headers;

int OnInit()
{
   EventSetTimer(InpTimerSeconds);
   g_headers = "Content-Type: application/json\r\nX-API-Key: " + InpApiKey + "\r\n";
   Print("AgentBridgeEA v2.00 | symbol=", InpSymbol,
         " M1=", InpBarsM1, " M5=", InpBarsM5, " M15=", InpBarsM15, " H1=", InpBarsH1);
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason) { EventKillTimer(); }

void OnTimer()
{
   if(!SymbolSelect(InpSymbol, true)) { Print("SymbolSelect failed: ", InpSymbol); return; }
   string payload = BuildSnapshotPayload(InpSymbol);
   if(payload == "") return;
   string ingest_resp = HttpPost("/v1/mt5/ingest", payload);
   if(StringLen(ingest_resp) == 0) return;
   string cmd_resp = HttpGet("/v1/mt5/next-command?symbol=" + InpSymbol);
   if(StringLen(cmd_resp) == 0) return;
   ExecuteCommandIfAny(cmd_resp);
}

// ── 采集单周期K线 ──────────────────────────────────────────────────
string BuildCandlesJson(string symbol, ENUM_TIMEFRAMES tf, int bars, int digits)
{
   MqlRates rates[];
   int copied = CopyRates(symbol, tf, 0, bars, rates);
   if(copied <= 0) { Print("CopyRates failed tf=", EnumToString(tf)); return "[]"; }
   ArraySetAsSeries(rates, true);

   string out = "[";
   for(int i = copied - 1; i >= 0; i--)
   {
      // 用短字段名 t/o/h/l/c/v 减少payload大小
      out += StringFormat(
         "{\"t\":\"%s\",\"o\":%.*f,\"h\":%.*f,\"l\":%.*f,\"c\":%.*f,\"v\":%d}",
         TimeToString(rates[i].time, TIME_DATE|TIME_SECONDS),
         digits, rates[i].open, digits, rates[i].high,
         digits, rates[i].low,  digits, rates[i].close,
         (int)rates[i].tick_volume
      );
      if(i != 0) out += ",";
   }
   return out + "]";
}

// ── 多周期快照 ────────────────────────────────────────────────────
string BuildSnapshotPayload(string symbol)
{
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick)) { Print("SymbolInfoTick failed"); return ""; }
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%.*f,\"ask\":%.*f,\"time\":\"%s\","
      "\"positions\":%s,"
      "\"candles_m1\":%s,"
      "\"candles_m5\":%s,"
      "\"candles_m15\":%s,"
      "\"candles_h1\":%s}",
      symbol, digits, tick.bid, digits, tick.ask,
      TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS),
      BuildPositionsJson(symbol, digits),
      BuildCandlesJson(symbol, PERIOD_M1,  InpBarsM1,  digits),
      BuildCandlesJson(symbol, PERIOD_M5,  InpBarsM5,  digits),
      BuildCandlesJson(symbol, PERIOD_M15, InpBarsM15, digits),
      BuildCandlesJson(symbol, PERIOD_H1,  InpBarsH1,  digits)
   );
   return payload;
}

// ── 持仓JSON ──────────────────────────────────────────────────────
string BuildPositionsJson(string symbol, int digits)
{
   string out = "[";
   bool first = true;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;
      if(!first) out += ",";
      first = false;
      out += StringFormat(
         "{\"ticket\":%I64u,\"type\":%d,\"volume\":%.2f,"
         "\"price_open\":%.*f,\"sl\":%.*f,\"tp\":%.*f,\"profit\":%.2f}",
         ticket, (int)PositionGetInteger(POSITION_TYPE), PositionGetDouble(POSITION_VOLUME),
         digits, PositionGetDouble(POSITION_PRICE_OPEN),
         digits, PositionGetDouble(POSITION_SL),
         digits, PositionGetDouble(POSITION_TP),
         PositionGetDouble(POSITION_PROFIT)
      );
   }
   return out + "]";
}

// ── 执行AI指令 ────────────────────────────────────────────────────
void ExecuteCommandIfAny(string raw)
{
   if(StringFind(raw, "\"action\":\"none\"") >= 0) return;
   string action = JsonExtract(raw, "action");
   if(action == "" || action == "none") return;
   string reason = JsonExtract(raw, "reason");

   double volume = StringToDouble(JsonExtract(raw, "volume"));
   double sl     = StringToDouble(JsonExtract(raw, "sl"));
   double tp     = StringToDouble(JsonExtract(raw, "tp"));
   double price  = StringToDouble(JsonExtract(raw, "price"));
   if(volume < 0.01) volume = 0.01;

   if(action == "close_all")
   {
      bool ok_all = CloseAllPositions(InpSymbol);
      Print("Position manage action=close_all ok=", ok_all);
      string result_close = StringFormat(
         "{\"ok\":%s,\"retcode\":0,\"comment\":\"close_all\",\"action\":\"%s\",\"volume\":0,"
         "\"sl\":0,\"tp\":0,\"exec_price\":0,\"ticket\":0,\"reason\":\"%s\"}",
         ok_all?"true":"false", action, reason
      );
      HttpPost("/v1/mt5/order-result", result_close);
      return;
   }
   if(action == "modify_all_sl_tp")
   {
      bool ok_mod = ModifyAllPositionsSLTP(InpSymbol, sl, tp);
      Print("Position manage action=modify_all_sl_tp ok=", ok_mod, " sl=", sl, " tp=", tp);
      int digits_mod = (int)SymbolInfoInteger(InpSymbol, SYMBOL_DIGITS);
      string result_mod = StringFormat(
         "{\"ok\":%s,\"retcode\":0,\"comment\":\"modify_all_sl_tp\",\"action\":\"%s\",\"volume\":0,"
         "\"sl\":%.*f,\"tp\":%.*f,\"exec_price\":0,\"ticket\":0,\"reason\":\"%s\"}",
         ok_mod?"true":"false", action, digits_mod, sl, digits_mod, tp, reason
      );
      HttpPost("/v1/mt5/order-result", result_mod);
      return;
   }

   MqlTradeRequest req; MqlTradeResult res;
   ZeroMemory(req); ZeroMemory(res);
   req.symbol = InpSymbol; req.volume = volume;
   req.sl = sl; req.tp = tp; req.deviation = 20;
   req.magic = 808888; req.type_filling = ORDER_FILLING_IOC;

   if(action == "buy_market")        { req.action=TRADE_ACTION_DEAL;    req.type=ORDER_TYPE_BUY;        req.price=SymbolInfoDouble(InpSymbol,SYMBOL_ASK); }
   else if(action == "sell_market")  { req.action=TRADE_ACTION_DEAL;    req.type=ORDER_TYPE_SELL;       req.price=SymbolInfoDouble(InpSymbol,SYMBOL_BID); }
   else if(action == "buy_limit")    { req.action=TRADE_ACTION_PENDING; req.type=ORDER_TYPE_BUY_LIMIT;  req.price=price; }
   else if(action == "sell_limit")   { req.action=TRADE_ACTION_PENDING; req.type=ORDER_TYPE_SELL_LIMIT; req.price=price; }
   else if(action == "buy_stop")     { req.action=TRADE_ACTION_PENDING; req.type=ORDER_TYPE_BUY_STOP;   req.price=price; }
   else if(action == "sell_stop")    { req.action=TRADE_ACTION_PENDING; req.type=ORDER_TYPE_SELL_STOP;  req.price=price; }
   else { Print("Unknown action: ", action); return; }

   bool ok = OrderSend(req, res);
   Print("OrderSend action=", action, " ok=", ok, " retcode=", res.retcode, " comment=", res.comment);

   int digits = (int)SymbolInfoInteger(InpSymbol, SYMBOL_DIGITS);
   // ── 上报完整信息供Python结构化存储 ──
   string result = StringFormat(
      "{\"ok\":%s,\"retcode\":%d,\"comment\":\"%s\","
      "\"action\":\"%s\",\"volume\":%.2f,"
      "\"sl\":%.*f,\"tp\":%.*f,\"exec_price\":%.*f,\"ticket\":%I64u,\"reason\":\"%s\"}",
      ok?"true":"false", res.retcode, res.comment,
      action, volume, digits, sl, digits, tp, digits, res.price, res.deal, reason
   );
   HttpPost("/v1/mt5/order-result", result);
}

bool CloseAllPositions(string symbol)
{
   bool ok_all = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;
      long ptype = PositionGetInteger(POSITION_TYPE);
      double vol = PositionGetDouble(POSITION_VOLUME);
      if(vol <= 0) continue;

      MqlTradeRequest req; MqlTradeResult res;
      ZeroMemory(req); ZeroMemory(res);
      req.action = TRADE_ACTION_DEAL;
      req.symbol = symbol;
      req.position = ticket;
      req.volume = vol;
      req.deviation = 20;
      req.magic = 808888;
      req.type_filling = ORDER_FILLING_IOC;

      if(ptype == POSITION_TYPE_BUY)
      {
         req.type = ORDER_TYPE_SELL;
         req.price = SymbolInfoDouble(symbol, SYMBOL_BID);
      }
      else
      {
         req.type = ORDER_TYPE_BUY;
         req.price = SymbolInfoDouble(symbol, SYMBOL_ASK);
      }

      bool ok = OrderSend(req, res);
      if(!ok) ok_all = false;
   }
   return ok_all;
}

bool ModifyAllPositionsSLTP(string symbol, double sl, double tp)
{
   bool ok_all = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0 || !PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;

      MqlTradeRequest req; MqlTradeResult res;
      ZeroMemory(req); ZeroMemory(res);
      req.action = TRADE_ACTION_SLTP;
      req.symbol = symbol;
      req.position = ticket;
      req.sl = sl;
      req.tp = tp;

      bool ok = OrderSend(req, res);
      if(!ok) ok_all = false;
   }
   return ok_all;
}

// ── HTTP ─────────────────────────────────────────────────────────
string HttpPost(string path, string body)
{
   uchar result[], data[];
   string result_headers;
   StringToCharArray(body, data, 0, StringLen(body), CP_UTF8);
   int code = WebRequest("POST", InpBridgeBaseUrl+path, g_headers, 5000, data, result, result_headers);
   if(code == -1)
   {
      int err = GetLastError();
      Print("POST error=", err, " path=", path);
      if(err == 4014) Print("请在 MT5->工具->选项->EA交易 中添加URL: ", InpBridgeBaseUrl);
      return "";
   }
   if(code != 200) Print("POST HTTP ", code, " ", StringSubstr(CharArrayToString(result),0,200));
   return CharArrayToString(result);
}

string HttpGet(string path)
{
   uchar result[], data[];
   string result_headers;
   int code = WebRequest("GET", InpBridgeBaseUrl+path, g_headers, 5000, data, result, result_headers);
   if(code == -1) { Print("GET error=", GetLastError(), " path=", path); return ""; }
   return CharArrayToString(result);
}

string JsonExtract(string src, string key)
{
   string k = "\"" + key + "\":";
   int pos = StringFind(src, k);
   if(pos < 0) return "";
   int start = pos + StringLen(k);
   while(start < StringLen(src) && StringGetCharacter(src, start) == ' ') start++;
   bool is_str = (StringGetCharacter(src, start) == '"');
   if(is_str) start++;
   int end = start;
   while(end < StringLen(src))
   {
      ushort c = StringGetCharacter(src, end);
      if(is_str) { if(c == '"') break; }
      else       { if(c==',' || c=='}' || c==']') break; }
      end++;
   }
   string v = StringSubstr(src, start, end-start);
   StringTrimLeft(v); StringTrimRight(v);
   return v;
}
