#property strict
#property version   "1.01"
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
   Print("AgentBridgeEA v1.01 initialized for symbol: ", InpSymbol);
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

   // 获取品种的小数位数，动态控制价格精度
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   MqlRates rates[];
   int copied = CopyRates(symbol, PERIOD_M1, 0, bars, rates);
   if(copied <= 0)
   {
      Print("CopyRates failed for ", symbol, " - copied=", copied);
      return "";
   }

   ArraySetAsSeries(rates, true);

   string candles = "[";
   for(int i = copied - 1; i >= 0; i--)
   {
      // tick_volume 用 %d（整数），real_volume 用 %.8f 保留精度
      candles += StringFormat(
         "{\"time\":\"%s\",\"open\":%.*f,\"high\":%.*f,\"low\":%.*f,\"close\":%.*f,\"tick_volume\":%d,\"real_volume\":%.8f}",
         TimeToString(rates[i].time, TIME_DATE|TIME_SECONDS),
         digits, rates[i].open,
         digits, rates[i].high,
         digits, rates[i].low,
         digits, rates[i].close,
         (int)rates[i].tick_volume,
         rates[i].real_volume
      );

      if(i != 0) candles += ",";
   }
   candles += "]";

   string positions = BuildPositionsJson(symbol, digits);

   string payload = StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%.*f,\"ask\":%.*f,\"time\":\"%s\",\"positions\":%s,\"candles_m1\":%s}",
      symbol,
      digits, tick.bid,
      digits, tick.ask,
      TimeToString(TimeCurrent(), TIME_DATE|TIME_SECONDS),
      positions,
      candles
   );

   return payload;
}

//+------------------------------------------------------------------+
//| 构建持仓JSON                                                     |
//+------------------------------------------------------------------+
string BuildPositionsJson(string symbol, int digits)
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
         "{\"ticket\":%I64u,\"type\":%d,\"volume\":%.2f,\"price_open\":%.*f,\"sl\":%.*f,\"tp\":%.*f,\"profit\":%.2f}",
         ticket,
         (int)PositionGetInteger(POSITION_TYPE),
         PositionGetDouble(POSITION_VOLUME),
         digits, PositionGetDouble(POSITION_PRICE_OPEN),
         digits, PositionGetDouble(POSITION_SL),
         digits, PositionGetDouble(POSITION_TP),
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
   if(StringFind(raw, "\"action\":\"none\"") >= 0) return;

   string action = JsonExtract(raw, "action");
   if(action == "" || action == "none") return;

   double volume = StringToDouble(JsonExtract(raw, "volume"));
   double sl     = StringToDouble(JsonExtract(raw, "sl"));
   double tp     = StringToDouble(JsonExtract(raw, "tp"));
   double price  = StringToDouble(JsonExtract(raw, "price"));

   if(volume < 0.01) volume = 0.01;

   MqlTradeRequest  req;
   MqlTradeResult   res;
   ZeroMemory(req);
   ZeroMemory(res);

   req.symbol       = InpSymbol;
   req.volume       = volume;
   req.sl           = sl;
   req.tp           = tp;
   req.deviation    = 20;
   req.magic        = 808888;
   req.type_filling = ORDER_FILLING_IOC;

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
      Print("Unknown action: ", action);
      return;
   }

   bool ok = OrderSend(req, res);
   Print("OrderSend action=", action, " ok=", ok, " retcode=", res.retcode, " comment=", res.comment);

   string result = StringFormat(
      "{\"ok\":%s,\"retcode\":%d,\"comment\":\"%s\",\"action\":\"%s\"}",
      ok ? "true" : "false",
      res.retcode,
      res.comment,
      action
   );

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

   // ✅ 修复：使用 StringLen(body) 而非 WHOLE_ARRAY
   // WHOLE_ARRAY 会把末尾的 \0 也放进 data，导致服务端收到非法 JSON -> 422
   StringToCharArray(body, data, 0, StringLen(body), CP_UTF8);

   int code = WebRequest("POST", InpBridgeBaseUrl + path, g_headers, 5000, data, result, result_headers);

   if(code == -1)
   {
      int err = GetLastError();
      Print("WebRequest POST error=", err, " path=", path);
      // 错误 4014: 需要在工具->选项->Expert Advisors 中允许 URL
      if(err == 4014) Print("请在 MT5 -> 工具 -> 选项 -> EA交易 中添加允许的URL: ", InpBridgeBaseUrl);
      return "";
   }
   if(code != 200)
   {
      Print("WebRequest POST HTTP ", code, " path=", path, " body_preview=", StringSubstr(CharArrayToString(result), 0, 200));
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
   uchar data[];

   int code = WebRequest("GET", InpBridgeBaseUrl + path, g_headers, 5000, data, result, result_headers);

   if(code == -1)
   {
      Print("WebRequest GET error=", GetLastError(), " path=", path);
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

   // 跳过空格
   while(start < StringLen(src) && StringGetCharacter(src, start) == ' ')
      start++;

   bool is_string = (StringGetCharacter(src, start) == '"');
   if(is_string) start++; // 跳过开头的引号

   int end = start;
   while(end < StringLen(src))
   {
      ushort c = StringGetCharacter(src, end);
      if(is_string)
      {
         if(c == '"') break; // 字符串值到结束引号为止
      }
      else
      {
         if(c == ',' || c == '}' || c == ']') break;
      }
      end++;
   }

   string value = StringSubstr(src, start, end - start);
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
}