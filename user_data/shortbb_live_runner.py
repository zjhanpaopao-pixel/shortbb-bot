# -*- coding: utf-8 -*-
"""ShortBB 全市场模拟交易执行器（统一策略版）。

- 直接调用与 Freqtrade 同一份 ShortBB.py 作为唯一策略口径（不再复制简化策略）。
- 数据源：币安测试网自身已收盘的 1h K 线（始终新鲜，排除尚未收盘的K线）。
- 选候选规则（文档第三节）：测试网允许交易 / 无持仓 / 同根K线未下单 / 总持仓<10；
  不做评分，按信号确认时间（同根K线按固定币种字母序）先到先得。
- 下单：逐仓、1倍杠杆、小额市价单；下单后回查币安校正本地记录；重启从币安同步持仓。
"""
import os, sys, json, time, math, logging
from pathlib import Path
import pandas as pd
import requests, hmac, hashlib
from urllib.parse import urlencode
import datetime

# 北京时间 (UTC+8) —— 所有面向用户的时间统一用北京时间，避免与币安 UTC 对不上
BJ = datetime.timezone(datetime.timedelta(hours=8))


def bj_now(sec=False):
    """当前北京时间字符串。"""
    fmt = "%Y-%m-%d %H:%M:%S" if sec else "%Y-%m-%d %H:%M"
    return datetime.datetime.now(BJ).strftime(fmt)


def bj_ms(ms):
    """币安 epoch 毫秒 -> 北京时间字符串；无效返回空串。"""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return ""
    if not ms:
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000, BJ).strftime("%Y-%m-%d %H:%M")

# 让执行器能 import 同一份 ShortBB.py
sys.path.insert(0, "/freqtrade/user_data/strategies")
from ShortBB import ShortBB  # noqa: E402

BASE = "https://testnet.binancefuture.com"
STATE = Path("/freqtrade/user_data/shortbb_live_state.json")
MONITOR = Path("/freqtrade/user_data/shortbb_monitor.html")
LOG = Path("/freqtrade/user_data/logs/shortbb-live.log")
MAX_POS = 10
STAKE = 100.0  # 每仓名义价值（USDT），测试网小额
DRY_RUN = os.environ.get("DRY_RUN", "1") == "1"
LOOP = os.environ.get("LOOP", "1") == "1"
FORCE = "--once" in sys.argv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(LOG), logging.StreamHandler()])
# 日志时间戳也统一用北京时间，便于排查
logging.Formatter.converter = lambda *args: datetime.datetime.now(BJ).timetuple()
log = logging.getLogger("shortbb-live")

KEY = os.environ["BINANCE_TESTNET_API_KEY"]
SECRET = os.environ["BINANCE_TESTNET_SECRET"]
session = requests.Session()
session.headers.update({"X-MBX-APIKEY": KEY})

strat = ShortBB(config={})


# ---------- 币安测试网 REST ----------
def api(method, path, params=None, signed=False):
    p = dict(params or {})
    if signed:
        p.update(timestamp=int(time.time() * 1000), recvWindow=60000)
        query = urlencode(p)
        p["signature"] = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    r = session.request(method, BASE + path, params=p, timeout=20)
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"{path} {r.status_code} {data}")
    return data


def set_margin_and_leverage(symbol):
    """开户前设逐仓 + 1倍杠杆（已设则忽略报错）。"""
    try:
        api("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, True)
    except RuntimeError as e:
        if "No need to change margin type" not in str(e):
            log.warning("marginType %s: %s", symbol, e)
    try:
        api("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": 1}, True)
    except RuntimeError as e:
        if "Leverage is not valid" not in str(e):
            log.warning("leverage %s: %s", symbol, e)


def order(symbol, side, quantity, reduce=False):
    return api("POST", "/fapi/v1/order",
               {"symbol": symbol, "side": side, "type": "MARKET",
                "quantity": f"{quantity:.8f}", "reduceOnly": str(reduce).lower(),
                "newOrderRespType": "RESULT"}, True)


# 稳定币不交易（价格锚定1，布林带会被噪音误触发）
STABLE = {"USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "UST", "USDD",
          "FRAX", "GUSD", "USDE", "PYUSD"}


def tradable_symbols():
    info = api("GET", "/fapi/v1/exchangeInfo")["symbols"]
    out, filt = [], {}
    for s in info:
        if s["contractType"] == "PERPETUAL" and s["quoteAsset"] == "USDT" and s["status"] == "TRADING":
            sym = s["symbol"]
            base = sym[:-4] if sym.endswith("USDT") else sym
            if sym.endswith("USDT") and "DOWN" not in sym and "UP" not in sym and base not in STABLE:
                out.append(sym)
                fs = {f["filterType"]: f for f in s["filters"]}
                step = float(fs["LOT_SIZE"]["stepSize"])
                min_qty = float(fs["LOT_SIZE"]["minQty"])
                mn = fs.get("MIN_NOTIONAL", {})
                min_notional = float(mn.get("notional", 0)) if isinstance(mn, dict) else 0
                filt[sym] = (step, min_qty, min_notional)
    return sorted(out), filt


def load_klines(symbol):
    raw = api("GET", "/fapi/v1/klines", {"symbol": symbol, "interval": "1h", "limit": 600})
    rows = raw[:-1]  # 丢弃还在形成的最后一根
    if len(rows) < 500:
        return None, None
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close",
                                      "volume", "close_time", "qv", "t", "tb", "tq", "ig"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["date"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    candle_id = int(df.iloc[-1]["close_time"])
    df = df[["date", "open", "high", "low", "close", "volume"]]
    df = strat.populate_indicators(df, {})
    df = strat.populate_entry_trend(df, {})
    df = strat.populate_exit_trend(df, {})
    return df, candle_id


def entry_reason(row, side):
    if side == "short":
        lvl = row["ref_low_short"] - 0.2 * row["ref_bw_short"]
        return (f"价格 ${row['close']:.4f} 跌破前期下轨破位位 ${lvl:.4f}（大阴线后），"
                f"位于布林中轨 ${row['bb_mid']:.4f} 下方、MA60 ${row['ma60']:.4f} 下方；"
                f"做空前置满足：前20日低点 ${row['short_rally_low']:.4f} 先于高点 ${row['short_rally_high']:.4f}，"
                f"涨幅 {row['short_rally_pct']:.0%}")
    lvl = row["ref_high_long"] + 0.2 * row["ref_bw_long"]
    return (f"价格 ${row['close']:.4f} 突破前期上轨突破位 ${lvl:.4f}（大阳线后），"
            f"位于布林中轨 ${row['bb_mid']:.4f} 上方、MA60 ${row['ma60']:.4f} 上方")


def exit_reason(row, side):
    atr = row["atr"]
    big_bull = row["body"] > 0 and abs(row["body"]) > 1.0 * atr
    big_bear = row["body"] < 0 and abs(row["body"]) > 1.0 * atr
    if side == "short":
        if big_bull and row["close"] > row["bb_mid"]:
            return "大阳线收盘站上布林中轨"
        if row["close"] > row["bb_up"] + 1.0 * atr:
            return "收盘价高于布林上轨+1倍ATR"
        if (row["close"] > row["bb_mid"] + 1.0 * atr) and not bool(row["flat_mid"]):
            return "中轨非走平，收盘价高于中轨+1倍ATR"
        if bool(row.get("bull_engulf_recent", False)):
            return "出现阳包阴（当前大阳线吞没近期大阴线）"
    else:
        if big_bear and row["close"] < row["bb_mid"]:
            return "大阴线收盘跌破布林中轨"
        if row["close"] < row["bb_low"] - 1.0 * atr:
            return "收盘价低于布林下轨-1倍ATR"
        if (row["close"] < row["bb_mid"] - 1.0 * atr) and not bool(row["flat_mid"]):
            return "中轨非走平，收盘价低于中轨-1倍ATR"
        if bool(row.get("bear_engulf_recent", False)):
            return "出现阴包阳（当前大阴线吞没近期大阳线）"
    return "反向信号"


def qty_for(symbol, price, filt):
    step, min_qty, min_notional = filt[symbol]
    qty = math.floor((STAKE / price) / step) * step
    qty = max(qty, min_qty)
    if qty * price < min_notional:
        return None
    return qty


def fetch_entry_time(symbol, amt):
    """从币安真实成交记录取当前持仓的开仓时间（北京时间）。

    多仓由买入开仓、空仓由卖出开仓，取最近一笔同方向成交的时间。
    取不到（网络/权限）时返回 None，由调用方兜底。
    """
    try:
        trades = api("GET", "/fapi/v1/userTrades", {"symbol": symbol, "limit": 50}, signed=True)
        side = "BUY" if amt > 0 else "SELL"
        for t in reversed(trades):
            if t.get("side") == side:
                return bj_ms(t.get("time"))
    except Exception as e:
        log.warning("userTrades %s 取开仓时间失败: %s", symbol, e)
    return None


# ---------- 主流程 ----------
def run_once():
    state = json.loads(STATE.read_text()) if STATE.exists() else {
        "positions": {}, "trades": [], "last_candle": {}, "scan": {}}
    symbols, filt = tradable_symbols()

    # 从币安实际持仓同步（重启安全 / 校正）
    actual = {}
    for p in api("GET", "/fapi/v2/positionRisk", signed=True):
        if p.get("symbol") not in symbols:
            continue
        try:
            amt = float(p["positionAmt"])
        except (KeyError, TypeError):
            continue
        if abs(amt) > 1e-9:
            actual[p["symbol"]] = amt
            cur = state["positions"].get(p["symbol"], {})
            if not cur.get("entry_time"):
                # 从币安真实成交取开仓时间（北京时间），取不到再用当前北京时间兜底
                cur["entry_time"] = fetch_entry_time(p["symbol"], amt) or bj_now()
            cur.update(side="long" if amt > 0 else "short",
                       entry_price=float(p.get("entryPrice", 0)),
                       qty=abs(amt), candle=cur.get("candle"))
            state["positions"][p["symbol"]] = cur
    # 币安已无持仓但本地还记着 -> 视为已平仓（外部平）
    for sym in list(state["positions"]):
        if sym not in actual:
            rec = state["positions"].pop(sym)
            state["trades"].append({**rec, "exit_time": "（币安已无持仓）",
                                    "exit_price": None, "pnl": None,
                                    "exit_reason": rec.get("exit_reason", "外部平仓/数据校正")})

    current_pos = len(actual)
    scanned, anomalies, candidates = 0, 0, []

    # 整点守卫：同根1h K线（且无持仓）时跳过全市场扫描，避免每60s刷500+请求
    new_candle = True
    if LOOP and not FORCE:
        try:
            probe = api("GET", "/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1h", "limit": 2})
            gc = int(probe[-2][6])  # 倒数第二根=刚收盘的
            if state.get("last_global_candle") == gc:
                new_candle = False
            else:
                state["last_global_candle"] = gc
        except Exception as e:
            log.warning("全局K线探针失败: %s", e)

    for sym in symbols:  # 固定字母序 = 同根K线内的处理顺序
        amt = actual.get(sym, 0.0)
        # 无持仓 且 非新K线 -> 不可能出新信号，跳过（省API）
        if abs(amt) <= 1e-9 and not new_candle:
            continue
        try:
            df, candle_id = load_klines(sym)
        except Exception as e:
            anomalies += 1
            log.warning("klines %s 失败: %s", sym, e)
            continue
        if df is None:
            continue
        scanned += 1
        row = df.iloc[-1]
        last = state["last_candle"].get(sym)

        # 已有持仓 -> 查四类出场
        if abs(amt) > 1e-9:
            side = "long" if amt > 0 else "short"
            sig = row["exit_long"] if side == "long" else row["exit_short"]
            if sig:
                reason = exit_reason(row, side)
                rec = state["positions"].get(sym, {})
                if not DRY_RUN:
                    try:
                        set_margin_and_leverage(sym)
                        order(sym, "SELL" if amt > 0 else "BUY", abs(amt), True)
                    except Exception as e:
                        log.warning("平仓 %s 失败: %s", sym, e)
                        candidates.append((sym, "平" + side, row["close"], reason, "下单失败"))
                        continue
                ep = rec.get("entry_price") or row["close"]
                pnl = (row["close"] - ep) * abs(amt) if side == "long" else (ep - row["close"]) * abs(amt)
                state["trades"].append({**rec, "exit_time": bj_ms(candle_id),
                                        "exit_price": float(row["close"]), "pnl": round(pnl, 4),
                                        "exit_reason": reason})
                state["positions"].pop(sym, None)
                current_pos -= 1
                candidates.append((sym, "平" + side, row["close"], reason,
                                   "演练-未下单" if DRY_RUN else "已平仓"))
            continue

        # 无持仓 -> 查入场
        if last == candle_id:
            continue  # 同根K线已处理过
        state["last_candle"][sym] = candle_id
        side = None
        if row["enter_short"]:
            side = "short"
        elif row["enter_long"]:
            side = "long"
        if not side:
            continue
        if current_pos >= MAX_POS:
            candidates.append((sym, side, row["close"], entry_reason(row, side), "达持仓上限"))
            continue
        reason = entry_reason(row, side)
        qty = qty_for(sym, row["close"], filt)
        if qty is None:
            candidates.append((sym, side, row["close"], reason, "测试网最小名义价值不足"))
            continue
        if DRY_RUN:
            candidates.append((sym, side, row["close"], reason, "演练-未下单"))
        else:
            try:
                set_margin_and_leverage(sym)
                order(sym, "BUY" if side == "long" else "SELL", qty, False)
                state["positions"][sym] = {"side": side, "entry_price": float(row["close"]),
                                           "entry_time": bj_ms(candle_id),
                                           "qty": qty, "reason": reason, "candle": candle_id}
                current_pos += 1
                candidates.append((sym, side, row["close"], reason, "已下单"))
            except Exception as e:
                log.warning("开仓 %s 失败: %s", sym, e)
                candidates.append((sym, side, row["close"], reason, "下单失败"))

    state["scan"] = {"time": bj_now(sec=True), "scanned": scanned,
                     "anomalies": anomalies, "positions": current_pos}
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    write_monitor(state, candidates)
    log.info("扫描 %d 币, 异常 %d, 当前持仓 %d, 本轮候选 %d", scanned, anomalies, current_pos, len(candidates))
    return state, candidates


def write_monitor(state, candidates):
    bal = 0.0
    try:
        for b in api("GET", "/fapi/v2/balance", signed=True):
            if b["asset"] == "USDT":
                bal = float(b["balance"])
    except Exception:
        pass
    sc = state.get("scan", {})
    used = sum(abs(float(p.get("entry_price", 0)) * p.get("qty", 0)) for p in state["positions"].values())
    rows_cand = "".join(
        f"<tr><td>{s}</td><td>{'做空' if d=='short' else '做多' if d=='long' else d}</td>"
        f"<td>{pr:.4f}</td><td style='text-align:left'>{r}</td><td>{st}</td></tr>"
        for (s, d, pr, r, st) in candidates) or "<tr><td colspan=5>本小时无候选</td></tr>"
    rows_pos = "".join(
        f"<tr><td>{s}</td><td>{'做多' if p['side']=='long' else '做空'}</td>"
        f"<td>{p.get('entry_time','')}</td><td>{p.get('entry_price','')}</td><td>{p.get('qty','')}</td>"
        f"<td style='text-align:left'>{p.get('reason','')}</td></tr>"
        for s, p in state["positions"].items()) or "<tr><td colspan=6>暂无持仓</td></tr>"
    rows_trd = "".join(
        f"<tr><td>{t.get('side','')}</td><td>{t.get('entry_time','')}</td><td>{t.get('entry_price','')}</td>"
        f"<td>{t.get('qty','')}</td><td>{t.get('exit_time','')}</td><td>{t.get('exit_price','')}</td>"
        f"<td>{t.get('pnl','')}</td><td style='text-align:left'>{t.get('exit_reason','')}</td></tr>"
        for t in state.get("trades", [])[-30:][::-1]) or "<tr><td colspan=8>暂无已平仓记录</td></tr>"

    html_doc = f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>ShortBB 模拟交易监控</title>
<style>
body{{font-family:-apple-system,Segoe UI,Roboto,'Microsoft YaHei',sans-serif;margin:20px;color:#222;background:#fafafa}}
h1{{font-size:20px}} h2{{font-size:15px;margin-top:24px;border-left:4px solid #c33;padding-left:8px}}
.status{{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0}}
.card{{background:#fff;border:1px solid #eee;border-radius:8px;padding:10px 14px;min-width:120px}}
.card b{{display:block;font-size:18px}} .card span{{color:#888;font-size:12px}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:13px;margin-top:8px}}
th,td{{border:1px solid #eee;padding:6px 8px;text-align:center}}
th{{background:#f3f3f3}} .dry{{color:#c33;font-weight:bold}}
</style></head><body>
<h1>ShortBB 全市场模拟交易监控</h1>
{'<p class="dry">⚠️ 当前为演练模式（DRY_RUN），未发送任何真实订单</p>' if DRY_RUN else ''}
<div class=status>
<div class=card><b>{sc.get('time','-')}</b><span>最近扫描时间</span></div>
<div class=card><b>{sc.get('scanned',0)}</b><span>扫描币种数</span></div>
<div class=card><b>{sc.get('anomalies',0)}</b><span>数据异常数</span></div>
<div class=card><b>{bal:.2f}</b><span>测试网 USDT 余额</span></div>
<div class=card><b>{used:.2f}</b><span>已用保证金</span></div>
<div class=card><b>{sc.get('positions',0)} / {MAX_POS}</b><span>当前持仓 / 上限</span></div>
</div>
<h2>本小时候选（按文档第三节 gating：测试网允许 / 无持仓 / 同K未下单 / &lt;10仓；先到先得不评分）</h2>
<table><tr><th>币种</th><th>方向</th><th>当前价</th><th>入场理由</th><th>最终状态</th></tr>{rows_cand}</table>
<h2>当前实际持仓</h2>
<table><tr><th>币种</th><th>方向</th><th>开仓时间</th><th>开仓价</th><th>数量</th><th>入场理由</th></tr>{rows_pos}</table>
<h2>已平仓记录（最近30条）</h2>
<table><tr><th>方向</th><th>开仓时间</th><th>开仓价</th><th>数量</th><th>平仓时间</th><th>平仓价</th><th>盈亏</th><th>出场理由</th></tr>{rows_trd}</table>
<p style="color:#aaa;font-size:12px">数据来源：币安测试网已收盘1h K线；策略：同一份 ShortBB.py（含做空前置+四类出场）。本页为本地记录与币安实际查询结果。</p>
</body></html>"""
    MONITOR.write_text(html_doc, encoding="utf-8")


def sleep_to_next_candle(interval_minutes=60):
    """睡到下一根 K 线收盘附近再醒，避免空转刷 API。"""
    try:
        now = datetime.now(timezone(timedelta(hours=8)))
        # 下一根 K 线收盘时间 = 当前小时向上取整到 interval 边界 + 2 分钟缓冲
        mins = ((now.minute // interval_minutes) + 1) * interval_minutes
        target = now.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=mins)
        # 如果已经过了目标（极端情况），就等下一个周期
        if target <= now:
            target += timedelta(minutes=interval_minutes)
        wait = max(10, int((target - now).total_seconds()))  # 至少 10 秒，防止死循环
        log.info("本轮扫描完成，等待 %d 秒后进入下一根 K 线（目标 %.02s:%.02s 北京）",
                 wait, target.strftime("%H:%M"))
        time.sleep(wait)
    except Exception:
        time.sleep(60)  # 兜底：计算失败时回退到 1 分钟


if __name__ == "__main__":
    log.info("ShortBB 执行器启动: testnet, 1h, DRY_RUN=%s, LOOP=%s", DRY_RUN, LOOP)
    if "--once" in sys.argv or not LOOP:
        run_once()
    else:
        while True:
            try:
                run_once()
            except Exception:
                log.exception("整点循环异常，将重试")
            sleep_to_next_candle()
