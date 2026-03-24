"""
Cálculos de indicadores técnicos: RSI, MACD, ATR, Volumen, señales de trading.
Módulo puro — sin estado, sin dependencias externas.
"""

from datetime import datetime

import pytz

RSI_PERIOD  = 14
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9


def calc_rsi(prices, period=RSI_PERIOD):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains  = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def ema(prices, period):
    if len(prices) < period:
        return prices[-1] if prices else 0
    k   = 2 / (period + 1)
    val = sum(prices[:period]) / period
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return val


def _ema_series(prices, period):
    """EMA incremental — O(n). Devuelve lista alineada desde índice period-1."""
    if len(prices) < period:
        return []
    k   = 2 / (period + 1)
    val = sum(prices[:period]) / period
    result = [val]
    for p in prices[period:]:
        val = p * k + val * (1 - k)
        result.append(val)
    return result


def calc_macd(prices):
    _empty = {'line': 0, 'signal': 0, 'hist': 0,
              'bullish': False, 'cross_up': False, 'cross_down': False}
    if len(prices) < MACD_SLOW + MACD_SIGNAL:
        return _empty

    fast = _ema_series(prices, MACD_FAST)  # len = n - MACD_FAST + 1
    slow = _ema_series(prices, MACD_SLOW)  # len = n - MACD_SLOW + 1
    # alinear: fast empieza MACD_FAST-1 antes que slow
    offset    = MACD_SLOW - MACD_FAST      # 14
    macd_line = [fast[offset + i] - slow[i] for i in range(len(slow))]

    if len(macd_line) < MACD_SIGNAL:
        return _empty
    sig       = ema(macd_line, MACD_SIGNAL)
    line      = macd_line[-1]
    hist      = line - sig
    prev_line = macd_line[-2] if len(macd_line) >= 2 else line
    prev_sig  = ema(macd_line[:-1], MACD_SIGNAL) if len(macd_line) > MACD_SIGNAL else sig
    return {
        'line':       round(line, 4),
        'signal':     round(sig,  4),
        'hist':       round(hist, 4),
        'bullish':    line > sig,
        'cross_up':   prev_line < prev_sig and line > sig,
        'cross_down': prev_line > prev_sig and line < sig,
    }


def calc_atr(bars, period=14):
    if len(bars) < 2:
        return 0
    trs = []
    for i in range(1, len(bars)):
        hi = bars[i].high
        lo = bars[i].low
        pc = bars[i - 1].close
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    if not trs:
        return 0
    return round(sum(trs[-period:]) / min(len(trs), period), 4)


def calc_volume_metrics(bars, period=20):
    if not bars:
        return {'volume': 0, 'avg_volume': 0, 'volume_ratio': 0.0, 'volume_signal': 'LOW'}
    volumes         = [int(getattr(bar, 'volume', 0) or 0) for bar in bars]
    current_volume  = volumes[-1] if volumes else 0
    baseline_source = volumes[-(period + 1):-1] if len(volumes) > 1 else []
    avg_volume      = (sum(baseline_source) / len(baseline_source)
                       if baseline_source else float(current_volume))
    ratio  = (current_volume / avg_volume) if avg_volume > 0 else 0.0
    signal = 'HIGH' if ratio >= 1.5 else 'NORMAL' if ratio >= 1.0 else 'LOW'
    return {
        'volume':        current_volume,
        'avg_volume':    int(round(avg_volume)),
        'volume_ratio':  round(ratio, 2),
        'volume_signal': signal,
    }


def signal_decision(rsi, macd, prices):
    if len(prices) < 3:
        return 'WAIT', {}
    buy_rsi    = rsi <= 45
    sell_rsi   = rsi >= 55
    buy_macd   = macd['bullish']
    sell_macd  = not macd['bullish']
    buy_candle  = prices[-1] > prices[-2] > prices[-3]
    sell_candle = prices[-1] < prices[-2] < prices[-3]
    signals = {
        'buy_rsi':    buy_rsi,    'sell_rsi':   sell_rsi,
        'buy_macd':   buy_macd,   'sell_macd':  sell_macd,
        'buy_candle': buy_candle, 'sell_candle': sell_candle,
    }
    buy_count  = sum([buy_rsi,  buy_macd,  buy_candle])
    sell_count = sum([sell_rsi, sell_macd, sell_candle])
    if buy_count == 3:
        decision = 'BUY'
    elif sell_count == 3:
        decision = 'SELL'
    elif buy_count == 2:
        decision = 'WATCH_BUY'
    elif sell_count == 2:
        decision = 'WATCH_SELL'
    else:
        decision = 'WAIT'
    return decision, signals


def consecutive_candles(prices):
    green = red = 0
    for i in range(len(prices) - 1, 0, -1):
        if prices[i] > prices[i - 1]:
            if red:
                break
            green += 1
        elif prices[i] < prices[i - 1]:
            if green:
                break
            red += 1
        else:
            break
    return green, red


def volume_label(volume_ratio):
    return 'con volumen' if volume_ratio >= 1.0 else 'sin volumen'


def timeframe_bias(rsi, macd, closes):
    if len(closes) < 3:
        return {'bias': 'NEUTRAL', 'score': 0, 'summary': 'Sin datos suficientes'}
    rising  = closes[-1] > closes[-2] > closes[-3]
    falling = closes[-1] < closes[-2] < closes[-3]
    score   = 0
    if rsi >= 55:
        score += 1
    elif rsi <= 45:
        score -= 1
    if macd.get('bullish'):
        score += 1
    else:
        score -= 1
    if rising:
        score += 1
    elif falling:
        score -= 1
    if score >= 2:
        bias, summary = 'BULLISH', 'Impulso alcista'
    elif score <= -2:
        bias, summary = 'BEARISH', 'Impulso bajista'
    else:
        bias, summary = 'NEUTRAL', 'Estructura mixta'
    return {'bias': bias, 'score': score, 'summary': summary}


def choose_trade_style(multi_tf):
    scalp_score = swing_score = 0
    one     = multi_tf.get('1m',  {})
    five    = multi_tf.get('5m',  {})
    fifteen = multi_tf.get('15m', {})
    four_h  = multi_tf.get('4h',  {})

    if one.get('bias') == five.get('bias') and one.get('bias') in ('BULLISH', 'BEARISH'):
        scalp_score += 2
    if one.get('score', 0) * five.get('score', 0) > 0:
        scalp_score += 1
    if abs(one.get('score', 0)) >= 2:
        scalp_score += 1

    if fifteen.get('bias') == four_h.get('bias') and fifteen.get('bias') in ('BULLISH', 'BEARISH'):
        swing_score += 2
    if fifteen.get('score', 0) * four_h.get('score', 0) > 0:
        swing_score += 1
    if abs(fifteen.get('score', 0)) >= 2:
        swing_score += 1

    structure = (f"Estructura 15m {fifteen['bias'].lower()}"
                 if fifteen.get('bias') in ('BULLISH', 'BEARISH')
                 else '15m sin estructura clara')

    if scalp_score >= swing_score + 1:
        style, note = 'SCALP', 'Mejor para scalp: 1m y 5m llevan la delantera.'
    elif swing_score >= scalp_score + 1:
        style, note = 'SWING', 'Mejor para swing: 15m y 4h están alineados.'
    else:
        style, note = 'WAIT', 'Sin ventaja clara entre scalp y swing.'

    return {
        'trade_style':    style,
        'scalp_score':    scalp_score,
        'swing_score':    swing_score,
        'structure_note': structure,
        'trade_note':     note,
    }


def session_name():
    london = datetime.now(pytz.timezone('Europe/London'))
    ny     = datetime.now(pytz.timezone('America/New_York'))
    london_open = 8 <= london.hour < 16
    ny_open     = 9 <= ny.hour     < 16
    if london_open and ny_open: return 'London+NY'
    if london_open:             return 'London'
    if ny_open:                 return 'New York'
    if 16 <= ny.hour < 21:      return 'NY cierre'
    return 'Asian'
