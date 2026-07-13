# -*- coding: utf-8 -*-
"""ShortBB v02 - symmetric long/short Bollinger confirmation strategy."""
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


class ShortBB(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "1h"
    can_short = True
    minimal_roi = {"0": 100.0}
    stoploss = -0.99
    use_exit_signal = True
    process_only_new_candles = True
    startup_candle_count = 90

    BB_PERIOD = 20
    BB_STD = 2.0
    MA_LONG = 60
    MA_SHORT = 5
    ATR_PERIOD = 14
    ENTRY_BODY_x_BW = 0.5
    REVERSAL_BODY_x_ATR = 1.0
    BREAK_x_BW = 0.2
    ENTRY_LAG = 5
    FLAT_LOOKBACK = 10
    FLAT_SLOPE_MAX = 0.005
    EXIT_UPPER_x_ATR = 1.0
    EXIT_MID_x_ATR = 1.0
    REVERSAL_LOOKBACK = 5
    SHORT_RALLY_DAYS = 20
    SHORT_RALLY_LOOKBACK = 20 * 24   # 480 根 1h K线 = 前20个自然日
    SHORT_RALLY_MIN = 1.0            # 低点->高点涨幅需 > 100% 才允许做空

    def leverage(self, pair, current_time, current_rate, proposed_leverage,
                 max_leverage, side, **kwargs):
        return 1.0

    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
        upper, mid, lower = ta.BBANDS(
            df["close"], timeperiod=self.BB_PERIOD,
            nbdevup=self.BB_STD, nbdevdn=self.BB_STD, matype=0)
        df["bb_up"], df["bb_mid"], df["bb_low"] = upper, mid, lower
        df["bb_bw"] = df["bb_up"] - df["bb_low"]
        df["ma60"] = ta.SMA(df["close"], timeperiod=self.MA_LONG)
        df["ma5"] = ta.SMA(df["close"], timeperiod=self.MA_SHORT)
        df["atr"] = ta.ATR(df["high"], df["low"], df["close"], timeperiod=self.ATR_PERIOD)
        df["body"] = df["close"] - df["open"]

        # 做空前置：前20日低点先于高点且涨幅>100%
        self._add_short_rally(df)

        n = self.FLAT_LOOKBACK
        df["flat_mid"] = (
            (df["bb_mid"] - df["bb_mid"].shift(n)).abs() / df["bb_mid"]
            <= self.FLAT_SLOPE_MAX
        )

        # The reference band is the band immediately before the setup candle.
        bear = (df["body"] < 0) & (df["body"].abs() > self.ENTRY_BODY_x_BW * df["bb_bw"].shift(1))
        bull = (df["body"] > 0) & (df["body"].abs() > self.ENTRY_BODY_x_BW * df["bb_bw"].shift(1))
        setup_short = bear & df["flat_mid"].fillna(False)
        setup_long = bull & df["flat_mid"].fillna(False)

        df["ref_low_short"] = np.where(setup_short, df["bb_low"].shift(1), np.nan)
        df["ref_high_long"] = np.where(setup_long, df["bb_up"].shift(1), np.nan)
        df["ref_bw_short"] = np.where(setup_short, df["bb_bw"].shift(1), np.nan)
        df["ref_bw_long"] = np.where(setup_long, df["bb_bw"].shift(1), np.nan)
        for col in ("ref_low_short", "ref_high_long", "ref_bw_short", "ref_bw_long"):
            df[col] = df[col].ffill(limit=self.ENTRY_LAG)

        # Look back five completed candles for an opposite large candle.
        big_bull = (df["body"] > 0) & (df["body"].abs() > self.REVERSAL_BODY_x_ATR * df["atr"])
        big_bear = (df["body"] < 0) & (df["body"].abs() > self.REVERSAL_BODY_x_ATR * df["atr"])
        df["recent_big_bear"] = big_bear.shift(1).rolling(self.REVERSAL_LOOKBACK, min_periods=1).max().fillna(0).astype(bool)
        df["recent_big_bull"] = big_bull.shift(1).rolling(self.REVERSAL_LOOKBACK, min_periods=1).max().fillna(0).astype(bool)
        # The current candle must engulf at least one opposite large candle in the lookback.
        df["bull_engulf_recent"] = False
        df["bear_engulf_recent"] = False
        for i in range(1, self.REVERSAL_LOOKBACK + 1):
            prev_body = df["body"].shift(i)
            prev_open = df["open"].shift(i)
            prev_close = df["close"].shift(i)
            df["bull_engulf_recent"] |= big_bull & (prev_body < 0) & (df["open"] <= prev_close) & (df["close"] >= prev_open)
            df["bear_engulf_recent"] |= big_bear & (prev_body > 0) & (df["open"] >= prev_close) & (df["close"] <= prev_open)
        return df

    def _add_short_rally(self, df: DataFrame) -> None:
        """做空前置：回看 SHORT_RALLY_LOOKBACK 根1h K线，要求低点先于高点出现，
        且低点到高点的涨幅超过 SHORT_RALLY_MIN（默认>100%）。满足才允许做空。"""
        look = self.SHORT_RALLY_LOOKBACK
        n = len(df)
        ok = np.zeros(n, dtype=bool)
        pct = np.zeros(n, dtype=float)
        lo = np.full(n, np.nan)
        hi = np.full(n, np.nan)
        if n > look:
            low = df["low"].to_numpy()
            high = df["high"].to_numpy()
            lw = sliding_window_view(low, look)   # 形状 (n-look+1, look)
            hw = sliding_window_view(high, look)
            lp = np.argmin(lw, axis=1)            # 窗口内最低价位置
            hp = np.argmax(hw, axis=1)            # 窗口内最高价位置
            lval = lw[np.arange(lw.shape[0]), lp]
            hval = hw[np.arange(hw.shape[0]), hp]
            rise = hval / lval - 1.0
            valid = (lp < hp) & (rise > self.SHORT_RALLY_MIN) & (lval > 0)
            # 窗口 k 覆盖 [k, k+look-1]，前置作用于其后的那根K线 j=k+look
            take = valid[: n - look]
            ok[look:n] = take
            pct[look:n] = np.where(take, rise[: n - look], 0.0)
            lo[look:n] = np.where(take, lval[: n - look], np.nan)
            hi[look:n] = np.where(take, hval[: n - look], np.nan)
        df["short_rally_ok"] = ok
        df["short_rally_pct"] = pct
        df["short_rally_low"] = lo
        df["short_rally_high"] = hi

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["enter_long"] = 0
        df["enter_short"] = 0
        short_level = df["ref_low_short"] - self.BREAK_x_BW * df["ref_bw_short"]
        long_level = df["ref_high_long"] + self.BREAK_x_BW * df["ref_bw_long"]
        short_break = (df["close"] < short_level) | (df["ma5"] < short_level)
        long_break = (df["close"] > long_level) | (df["ma5"] > long_level)
        short = (df["ref_low_short"].notna() & short_break & (df["close"] < df["bb_mid"]) &
                 (df["close"] < df["ma60"]) & df["short_rally_ok"].fillna(False))
        long = df["ref_high_long"].notna() & long_break & (df["close"] > df["bb_mid"]) & (df["close"] > df["ma60"])
        df.loc[short, "enter_short"] = 1
        df.loc[short, "enter_tag"] = "大阴线破下轨"
        df.loc[long, "enter_long"] = 1
        df.loc[long, "enter_tag"] = "大阳线破上轨"
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        atr = df["atr"]
        big_bull = (df["body"] > 0) & (df["body"].abs() > self.REVERSAL_BODY_x_ATR * atr)
        big_bear = (df["body"] < 0) & (df["body"].abs() > self.REVERSAL_BODY_x_ATR * atr)

        # Ordinary flat-band crossings of the middle line do not close a trade.
        exit_short = (
            (big_bull & (df["close"] > df["bb_mid"])) |
            (df["close"] > df["bb_up"] + self.EXIT_UPPER_x_ATR * atr) |
            ((df["close"] > df["bb_mid"] + self.EXIT_MID_x_ATR * atr) & ~df["flat_mid"]) |
            df["bull_engulf_recent"]
        )
        exit_long = (
            (big_bear & (df["close"] < df["bb_mid"])) |
            (df["close"] < df["bb_low"] - self.EXIT_UPPER_x_ATR * atr) |
            ((df["close"] < df["bb_mid"] - self.EXIT_MID_x_ATR * atr) & ~df["flat_mid"]) |
            df["bear_engulf_recent"]
        )
        df["exit_short"] = exit_short.astype(int)
        df["exit_long"] = exit_long.astype(int)
        df.loc[exit_short, "exit_tag"] = "反向强势或阳包阴"
        df.loc[exit_long, "exit_tag"] = "反向强势或阴包阳"
        return df
