#property strict
#property version   "1.00"
#property description "Bridge MT5 data to external AI agent via HTTP"

input string InpBridgeBaseUrl = "http://127.0.0.1:8000";
input string InpApiKey       = "change_me";
input string InpSymbol       = "BTCUSD";
input int    InpTimerSeconds = 1;
input int    InpMaxBars      = 120;

string g_headers;

//+------------------------------------------------------------------+
//| Expert initialization function                                   |
//+------------------------------------------------------------------+
int OnInit()
{
   EventSetTimer(InpTimerSeconds);
   g_headers = "Content-Type: application/json\r\nX-API-Key: " + InpApiKey + "\r\n";
   Print("AgentBridgeEA initialized");
   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   EventKillTimer();
}

//+------------------------------------------------------------------+
//| Timer function                                                   |
//+------------------------------------------------------------------+
void OnTimer()
{
   if(!SymbolSelect(InpSymbol, true))
   {
      Print("Failed to select symbol: ", InpSymbol);
      return;
   }

   string payload = BuildSnapshotPayload(InpSymbol, InpMaxBars);
   if(payload == "") return;

   string ingest_resp = HttpPost("/v1/mt5/ingest", payload);
   if(StringLen(ingest_resp) == 0) return;

   string cmd_resp = HttpGet("/v1/mt5/next-command?symbol=" + InpSymbol);
   if(StringLen(cmd_resp) == 0) return;

   ExecuteCommandIfAny(cmd_resp);
}

//+------------------------------------------------------------------+
//| 构建快照数据                                                     |
//+------------------------------------------------------------------+
string BuildSnapshotPayload(string symbol, int bars)
{
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
   {
      Print("SymbolInfoTick failed for ", symbol);
      return "";
   }

   MqlRates rates[];
   int copied = CopyRates(symbol, PERIOD_M1, 0, bars, rates);
   if(copied <= 0)
   {
      Print("CopyRates failed for ", symbol);
      return "";
   }

   ArraySetAsSeries(rates, true);

   string candles = "[";
   for(int i = copied - 1; i >= 0; i--)
   {
      candles += StringFormat("{\"time\":\"%s\",\"open\":%.2f,\"high\":%.2f,\"low\":%.2f,\"close\":%.2f,\"tick_volume\":%I64d}",
                              TimeToString(rates[i].time, TIME_DATE|TIME_SECONDS),
                              rates[i].open, rates[i].high, rates[i].low, rates[i].close, 
                              rates[i].tick_volume);

      if(i != 0) candles += ",";
   }
   candles += "]";

   string positions = BuildPositionsJson(symbol);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%.2f,\"ask\":%.2f,\"time\":\"%s\",\"positions\":%s,\"candles_m1\":%s}",
      symbol, tick.bid, tick.ask,
      TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS),
      positions,
      candles
   );

   return payload;
}

//+------------------------------------------------------------------+
//| 构建持仓JSON                                                     |
//+------------------------------------------------------------------+
string BuildPositionsJson(string symbol)
{
   string out = "[";
   bool first = true;

   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(!PositionSelectByTicket(ticket)) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;

      if(!first) out += ",";
      first = false;

      out += StringFormat(
         "{\"ticket\":%I64u,\"type\":%d,\"volume\":%.2f,\"price_open\":%.2f,\"sl\":%.2f,\"tp\":%.2f,\"profit\":%.2f}",
         ticket,
         (int)PositionGetInteger(POSITION_TYPE),
         PositionGetDouble(POSITION_VOLUME),
         PositionGetDouble(POSITION_PRICE_OPEN),
         PositionGetDouble(POSITION_SL),
         PositionGetDouble(POSITION_TP),
         PositionGetDouble(POSITION_PROFIT)
      );
   }
   out += "]";
   return out;
}

//+------------------------------------------------------------------+
//| 执行从AI返回的交易指令                                           |
//+------------------------------------------------------------------+
void ExecuteCommandIfAny(string raw)
{
   // 如果返回的是 none 则直接退出
   if(StringFind(raw, "\"action\":\"none\"") >= 0) return;

   string action = JsonExtract(raw, "action");
   if(action == "") return;

   double volume = StringToDouble(JsonExtract(raw, "volume"));
   double sl     = StringToDouble(JsonExtract(raw, "sl"));
   double tp     = StringToDouble(JsonExtract(raw, "tp"));
   double price  = StringToDouble(JsonExtract(raw, "price"));

   if(volume < 0.01) volume = 0.01;

   MqlTradeRequest  req;
   MqlTradeResult   res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.symbol     = InpSymbol;
   req.volume     = volume;
   req.sl         = sl;
   req.tp         = tp;
   req.deviation  = 20;
   req.magic      = 808888;
   req.type_filling = ORDER_FILLING_IOC;   // 建议加上

   if(action == "buy_market")
   {
      req.action = TRADE_ACTION_DEAL;
      req.type   = ORDER_TYPE_BUY;
      req.price  = SymbolInfoDouble(InpSymbol, SYMBOL_ASK);
   }
   else if(action == "sell_market")
   {
      req.action = TRADE_ACTION_DEAL;
      req.type   = ORDER_TYPE_SELL;
      req.price  = SymbolInfoDouble(InpSymbol, SYMBOL_BID);
   }
   else if(action == "buy_limit")
   {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_BUY_LIMIT;
      req.price  = price;
   }
   else if(action == "sell_limit")
   {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_SELL_LIMIT;
      req.price  = price;
   }
   else if(action == "buy_stop")
   {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_BUY_STOP;
      req.price  = price;
   }
   else if(action == "sell_stop")
   {
      req.action = TRADE_ACTION_PENDING;
      req.type   = ORDER_TYPE_SELL_STOP;
      req.price  = price;
   }
   else
   {
      return;
   }

   bool ok = OrderSend(req, res);

   string result = StringFormat("{\"ok\":%s,\"retcode\":%d,\"comment\":\"%s\",\"action\":\"%s\"}",
                                ok ? "true" : "false",
                                res.retcode,
                                res.comment,
                                action);

   HttpPost("/v1/mt5/order-result", result);
}

//+------------------------------------------------------------------+
//| HTTP POST                                                        |
//+------------------------------------------------------------------+
string HttpPost(string path, string body)
{
   uchar result[];
   string result_headers;
   uchar data[];

   StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);

   int code = WebRequest("POST", InpBridgeBaseUrl + path, g_headers, 5000, data, result, result_headers);

   if(code == -1)
   {
      Print("WebRequest POST error: ", GetLastError());
      return "";
   }
   return CharArrayToString(result);
}

//+------------------------------------------------------------------+
//| HTTP GET                                                         |
//+------------------------------------------------------------------+
string HttpGet(string path)
{
   uchar result[];
   string result_headers;
   uchar data[];   // GET 不需要 body

   int code = WebRequest("GET", InpBridgeBaseUrl + path, g_headers, 5000, data, result, result_headers);

   if(code == -1)
   {
      Print("WebRequest GET error: ", GetLastError());
      return "";
   }
   return CharArrayToString(result);
}

//+------------------------------------------------------------------+
//| 简易 JSON 提取（仅适用于本EA的简单结构）                        |
//+------------------------------------------------------------------+
string JsonExtract(string src, string key)
{
   string k = "\"" + key + "\":";
   int pos = StringFind(src, k);
   if(pos < 0) return "";

   int start = pos + StringLen(k);

   // 跳过空格和引号
   while(start < StringLen(src))
   {
      ushort c = StringGetCharacter(src, start);
      if(c != ' ' && c != '"' && c != ':') break;
      start++;
   }

   int end = start;
   while(end < StringLen(src))
   {
      ushort c = StringGetCharacter(src, end);
      if(c == ',' || c == '}' || c == ']') break;
      end++;
   }

   string value = StringSubstr(src, start, end - start);
   StringTrimLeft(value);
   StringTrimRight(value);

   // 去掉可能残留的引号（如果是字符串）
   if(StringLen(value) >= 2 && StringGetCharacter(value,0)=='"' && StringGetCharacter(value,StringLen(value)-1)=='"')
      value = StringSubstr(value, 1, StringLen(value)-2);

   return value;
}