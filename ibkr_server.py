"""
IBKR Dashboard Server
Conecta a TWS Live (puerto 7496), sincroniza posiciones abiertas,
recibe market data en streaming con ib_insync y sirve JSON para el dashboard.

Uso:
    pip install ib_insync==0.9.70
    python3 ibkr_server.py
"""

import asyncio
import json
import math
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

from ib_insync import IB, Ticker, util

# CONFIG
HOST = '127.0.0.1'
PORT = 8766
TWS_HOST = '127.0.0.1'
TWS_PORT = 7496
CLIENT_ID = 42
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
BAR_SIZE = '5 mins'
HISTORICAL_DURATION = '2 D'
MULTI_TF_CONFIG = {
    '1m': {'duration': '1 D', 'bar_size': '1 min'},
    '5m': {'duration': '2 D', 'bar_size': '5 mins'},
    '15m': {'duration': '5 D', 'bar_size': '15 mins'},
    '4h': {'duration': '90 D', 'bar_size': '4 hours'},
}
SYNC_INTERVAL = 3
ANALYTICS_INTERVAL = 3
LOOP_TIMEOUT = 20
ALL_POSITIONS_REFRESH_INTERVAL = 30

ib = IB()
ib_loop = None
market_data_type = 3
market_data_label = 'Delayed 15min'
state_lock = threading.Lock()
background_threads_started = False

state = {
    'connected': False,
    'positions': {},
    'last_update': 0,
    'data_mode': market_data_label,
    'selected_symbol': 'ALL',
}

price_history = defaultdict(lambda: deque(maxlen=200))
subscriptions = {}


# ── INDICADORES ────────────────────────────────────────────────

def calc_rsi(prices, period=RSI_PERIOD):
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [d if d > 0 else 0 for d in deltas]
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
    k = 2 / (period + 1)
    val = sum(prices[:period]) / period
    for p in prices[period:]:
        val = p * k + val * (1 - k)
    return val


def calc_macd(prices):
    if len(prices) < MACD_SLOW + MACD_SIGNAL:
        return {
            'line': 0,
            'signal': 0,
            'hist': 0,
            'bullish': False,
            'cross_up': False,
            'cross_down': False,
        }
    macd_line = []
    for i in range(MACD_SLOW - 1, len(prices)):
        chunk = prices[:i + 1]
        fast = ema(chunk, MACD_FAST)
        slow = ema(chunk, MACD_SLOW)
        macd_line.append(fast - slow)
    if len(macd_line) < MACD_SIGNAL:
        return {
            'line': 0,
            'signal': 0,
            'hist': 0,
            'bullish': False,
            'cross_up': False,
            'cross_down': False,
        }
    sig = ema(macd_line, MACD_SIGNAL)
    line = macd_line[-1]
    hist = line - sig
    prev_line = macd_line[-2] if len(macd_line) >= 2 else line
    prev_sig = ema(macd_line[:-1], MACD_SIGNAL) if len(macd_line) > MACD_SIGNAL else sig
    cross_up = prev_line < prev_sig and line > sig
    cross_down = prev_line > prev_sig and line < sig
    return {
        'line': round(line, 4),
        'signal': round(sig, 4),
        'hist': round(hist, 4),
        'bullish': line > sig,
        'cross_up': cross_up,
        'cross_down': cross_down,
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
        return {
            'volume': 0,
            'avg_volume': 0,
            'volume_ratio': 0.0,
            'volume_signal': 'LOW',
        }
    volumes = [int(getattr(bar, 'volume', 0) or 0) for bar in bars]
    current_volume = volumes[-1] if volumes else 0
    baseline_source = volumes[-(period + 1):-1] if len(volumes) > 1 else []
    avg_volume = sum(baseline_source) / len(baseline_source) if baseline_source else float(current_volume)
    ratio = (current_volume / avg_volume) if avg_volume > 0 else 0.0
    if ratio >= 1.5:
        signal = 'HIGH'
    elif ratio >= 1.0:
        signal = 'NORMAL'
    else:
        signal = 'LOW'
    return {
        'volume': current_volume,
        'avg_volume': int(round(avg_volume)),
        'volume_ratio': round(ratio, 2),
        'volume_signal': signal,
    }


def signal_decision(rsi, macd, prices):
    if len(prices) < 3:
        return 'WAIT', {}
    buy_rsi = rsi <= 45
    sell_rsi = rsi >= 55
    buy_macd = macd['bullish']
    sell_macd = not macd['bullish']
    buy_candle = prices[-1] > prices[-2] > prices[-3]
    sell_candle = prices[-1] < prices[-2] < prices[-3]
    signals = {
        'buy_rsi': buy_rsi,
        'sell_rsi': sell_rsi,
        'buy_macd': buy_macd,
        'sell_macd': sell_macd,
        'buy_candle': buy_candle,
        'sell_candle': sell_candle,
    }
    buy_count = sum([buy_rsi, buy_macd, buy_candle])
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
    green = 0
    red = 0
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
        return {
            'bias': 'NEUTRAL',
            'score': 0,
            'summary': 'Sin datos suficientes',
        }

    rising = closes[-1] > closes[-2] > closes[-3]
    falling = closes[-1] < closes[-2] < closes[-3]
    score = 0

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
        bias = 'BULLISH'
        summary = 'Impulso alcista'
    elif score <= -2:
        bias = 'BEARISH'
        summary = 'Impulso bajista'
    else:
        bias = 'NEUTRAL'
        summary = 'Estructura mixta'

    return {
        'bias': bias,
        'score': score,
        'summary': summary,
    }


def choose_trade_style(multi_tf):
    scalp_score = 0
    swing_score = 0

    one = multi_tf.get('1m', {})
    five = multi_tf.get('5m', {})
    fifteen = multi_tf.get('15m', {})
    four_h = multi_tf.get('4h', {})

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

    if fifteen.get('bias') in ('BULLISH', 'BEARISH'):
        structure = f"Estructura 15m {fifteen['bias'].lower()}"
    else:
        structure = '15m sin estructura clara'

    if scalp_score >= swing_score + 1:
        style = 'SCALP'
        note = 'Mejor para scalp: 1m y 5m llevan la delantera.'
    elif swing_score >= scalp_score + 1:
        style = 'SWING'
        note = 'Mejor para swing: 15m y 4h están alineados.'
    else:
        style = 'WAIT'
        note = 'Sin ventaja clara entre scalp y swing.'

    return {
        'trade_style': style,
        'scalp_score': scalp_score,
        'swing_score': swing_score,
        'structure_note': structure,
        'trade_note': note,
    }


def session_name():
    u = datetime.utcnow().hour
    if 13 <= u < 16:
        return 'London+NY'
    if 8 <= u < 13:
        return 'London'
    if 16 <= u < 21:
        return 'New York'
    if u >= 21 or u < 2:
        return 'NY cierre'
    return 'Asian'


# ── HELPERS ────────────────────────────────────────────────────

def is_valid_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return not math.isnan(number) and not math.isinf(number)


def normalize_price(value):
    if not is_valid_number(value):
        return 0.0
    number = float(value)
    return number if number > 0 else 0.0


def get_market_price(ticker: Ticker):
    for candidate in (getattr(ticker, 'last', None), ticker.marketPrice()):
        price = normalize_price(candidate)
        if price > 0:
            return price
    bid = normalize_price(getattr(ticker, 'bid', None))
    ask = normalize_price(getattr(ticker, 'ask', None))
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    close = normalize_price(getattr(ticker, 'close', None))
    if close > 0:
        return close
    return 0.0


def get_bid_ask(ticker: Ticker, price=0.0):
    bid = normalize_price(getattr(ticker, 'bid', None))
    ask = normalize_price(getattr(ticker, 'ask', None))
    if bid == 0:
        bid = price
    if ask == 0:
        ask = price
    return rounded(bid), rounded(ask)


def rounded(value, digits=4):
    return round(value, digits) if is_valid_number(value) else 0.0


def run_on_ib_loop(func, timeout=LOOP_TIMEOUT):
    if ib_loop is None:
        raise RuntimeError('IB event loop no inicializado')
    if threading.current_thread() is threading.main_thread():
        return func()
    future = Future()

    def runner():
        try:
            future.set_result(func())
        except Exception as exc:
            future.set_exception(exc)

    ib_loop.call_soon_threadsafe(runner)
    return future.result(timeout=timeout)


def run_ib_coro(coro, timeout=LOOP_TIMEOUT):
    if ib_loop is None:
        raise RuntimeError('IB event loop no inicializado')
    if threading.current_thread() is threading.main_thread() and not ib_loop.is_running():
        return ib_loop.run_until_complete(coro)
    future = asyncio.run_coroutine_threadsafe(coro, ib_loop)
    return future.result(timeout=timeout)


def build_position_entry(symbol, qty, avg_cost, contract):
    return {
        'symbol': symbol,
        'price': 0.0,
        'bid': 0.0,
        'ask': 0.0,
        'qty': qty,
        'avg_cost': rounded(avg_cost),
        'pnl_per_share': 0.0,
        'pnl_total': 0.0,
        'pnl_pct': 0.0,
        'rsi': 50.0,
        'rsi_rising': False,
        'rsi_falling': False,
        'rsi_data': {
            'value': 50.0,
            'rising': False,
            'falling': False,
        },
        'macd': {
            'line': 0,
            'signal': 0,
            'hist': 0,
            'bullish': False,
            'cross_up': False,
            'cross_down': False,
        },
        'candles': {
            'green_consecutive': 0,
            'red_consecutive': 0,
        },
        'atr': 0.0,
        'volume': 0,
        'avg_volume': 0,
        'volume_ratio': 0.0,
        'volume_signal': 'LOW',
        'vol_label': 'sin volumen',
        'sl_suggested': 0.0,
        'tp1_suggested': 0.0,
        'decision': 'WAIT',
        'trade_style': 'WAIT',
        'trade_note': 'Sin ventaja clara entre scalp y swing.',
        'structure_note': '15m sin estructura clara',
        'multi_tf': {},
        'signals': {},
        'timestamp': time.time(),
        'session': session_name(),
        'data_mode': market_data_label,
        'contract_id': getattr(contract, 'conId', 0),
    }


def update_position_basics(symbol, qty, avg_cost, contract):
    with state_lock:
        position = state['positions'].get(symbol)
        if position is None:
            position = build_position_entry(symbol, qty, avg_cost, contract)
            state['positions'][symbol] = position
        position['qty'] = qty
        position['avg_cost'] = rounded(avg_cost)
        position['session'] = session_name()
        position['data_mode'] = market_data_label
        position['contract_id'] = getattr(contract, 'conId', 0)


def apply_price_update(position, price, bid, ask, timestamp=None):
    now = timestamp or time.time()
    if price > 0:
        position['price'] = rounded(price)
    if bid > 0:
        position['bid'] = rounded(bid)
    elif price > 0:
        position['bid'] = rounded(price)
    if ask > 0:
        position['ask'] = rounded(ask)
    elif price > 0:
        position['ask'] = rounded(price)
    position['timestamp'] = now
    position['session'] = session_name()
    position['data_mode'] = market_data_label
    recalc_pnl_locked(position)


def recalc_pnl_locked(position):
    price = normalize_price(position.get('price'))
    avg_cost = normalize_price(position.get('avg_cost'))
    qty = position.get('qty', 0)
    pnl_per_share = (price - avg_cost) if price > 0 and avg_cost > 0 else 0.0
    pnl_total = pnl_per_share * qty
    pnl_pct = (pnl_per_share / avg_cost * 100) if avg_cost > 0 else 0.0
    position['pnl_per_share'] = rounded(pnl_per_share)
    position['pnl_total'] = round(pnl_total, 2)
    position['pnl_pct'] = round(pnl_pct, 2)


def snapshot_state():
    with state_lock:
        return {
            'connected': state['connected'],
            'last_update': state['last_update'],
            'session': session_name(),
            'positions': [deepcopy(pos) for pos in state['positions'].values()],
            'timestamp': time.time(),
            'data_mode': state.get('data_mode', market_data_label),
            'selected_symbol': state.get('selected_symbol', 'ALL'),
        }


def snapshot_position(symbol):
    with state_lock:
        position = state['positions'].get(symbol)
        return deepcopy(position) if position else None


def analysis_payload(symbol=None):
    with state_lock:
        selected = symbol or state.get('selected_symbol', 'ALL')
        if selected == 'ALL' and state['positions']:
            selected = sorted(state['positions'].keys())[0]
        position = deepcopy(state['positions'].get(selected)) if selected and selected in state['positions'] else None

    if not position:
        return {
            'general': 'Sin símbolo seleccionado.',
            'spike': '',
            'vol_label': 'sin volumen',
            'session': session_name(),
            'timestamp': time.time(),
        }

    decision = position.get('decision', 'WAIT')
    trade_style = position.get('trade_style', 'WAIT')
    structure = position.get('structure_note', '15m sin estructura clara')
    vol = position.get('vol_label', 'sin volumen')

    if decision == 'BUY':
        general = f"{position['symbol']}: sesgo comprador. {structure}. Mejor para {trade_style.lower()}."
    elif decision == 'SELL':
        general = f"{position['symbol']}: sesgo vendedor. {structure}. Mejor para {trade_style.lower()}."
    elif decision in ('WATCH_BUY', 'WATCH_SELL'):
        general = f"{position['symbol']}: vigilancia activa. {position.get('trade_note', '')}"
    else:
        general = f"{position['symbol']}: sin confirmación clara. {position.get('trade_note', '')}"

    return {
        'general': general.strip(),
        'spike': '',
        'vol_label': vol,
        'session': position.get('session', session_name()),
        'timestamp': time.time(),
        'trade_style': trade_style,
        'decision': decision,
        'structure_note': structure,
    }


def get_selected_symbol():
    with state_lock:
        selected = state.get('selected_symbol', 'ALL')
    return selected if selected and selected != 'ALL' else None


def set_selected_symbol(symbol):
    symbol = (symbol or 'ALL').strip().upper()

    with state_lock:
        existing_positions = set(state['positions'].keys())
        normalized = symbol if symbol in existing_positions else 'ALL'
        previous = state.get('selected_symbol', 'ALL')
        state['selected_symbol'] = normalized

    if previous != normalized:
        print(f'[FOCUS] simbolo activo: {normalized}')

    sync_positions()
    if normalized == 'ALL':
        try:
            refresh_all_positions_once()
        except Exception as exc:
            print(f'[FOCUS] ALL refresh error: {exc}')
    return normalized


# ── IBKR STREAMING ─────────────────────────────────────────────

def connect_ibkr():
    global ib_loop

    print('[IBKR] Conectando a TWS...')
    ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
    ib.reqMarketDataType(market_data_type)
    ib_loop = util.getLoop()

    with state_lock:
        state['connected'] = ib.isConnected()
        state['data_mode'] = market_data_label

    print(f'[IBKR] Conectado | Modo de datos: {market_data_label}')


def subscribe_symbol(sym, qty, avg_cost, contract):
    def subscribe():
        ticker = ib.reqMktData(contract, '', False, False)
        with state_lock:
            subscriptions[sym] = {
                'contract': contract,
                'ticker': ticker,
            }
        return ticker

    update_position_basics(sym, qty, avg_cost, contract)
    run_on_ib_loop(subscribe)
    print(f'[SUB] {sym} suscrito')


def unsubscribe_symbol(sym, remove_position=False):
    with state_lock:
        subscription = subscriptions.pop(sym, None)
        if remove_position:
            state['positions'].pop(sym, None)
            price_history.pop(sym, None)

    if not subscription:
        return

    contract = subscription['contract']

    def unsubscribe():
        ib.cancelMktData(contract)

    try:
        run_on_ib_loop(unsubscribe)
    except Exception as exc:
        print(f'[UNSUB] {sym} error al cancelar market data: {exc}')
    else:
        print(f'[UNSUB] {sym} removido')


def fetch_positions():
    return run_on_ib_loop(lambda: list(ib.positions()))


def sync_positions():
    positions = fetch_positions()
    current = {}

    for pos in positions:
        contract = pos.contract
        if contract.secType != 'STK':
            continue
        if pos.position == 0:
            continue
        sym = contract.symbol
        current[sym] = {
            'qty': pos.position,
            'avg_cost': pos.avgCost,
            'contract': contract,
        }

    with state_lock:
        existing_symbols = set(subscriptions.keys())
        selected_symbol = state.get('selected_symbol', 'ALL')

    current_symbols = set(current.keys())
    active_symbol = selected_symbol if selected_symbol in current_symbols else None

    if active_symbol is None and selected_symbol != 'ALL':
        with state_lock:
            state['selected_symbol'] = 'ALL'

    for sym in sorted(current_symbols):
        info = current[sym]
        update_position_basics(sym, info['qty'], info['avg_cost'], info['contract'])

    desired_subscriptions = {active_symbol} if active_symbol else set()

    for sym in sorted(desired_subscriptions - existing_symbols):
        info = current[sym]
        subscribe_symbol(sym, info['qty'], info['avg_cost'], info['contract'])

    for sym in sorted(existing_symbols - desired_subscriptions):
        unsubscribe_symbol(sym)

    if selected_symbol == 'ALL' and not existing_symbols:
        for sym in sorted(current_symbols):
            position = snapshot_position(sym)
            if position and position.get('price', 0) > 0:
                continue
            try:
                refresh_position_snapshot(sym, current[sym]['contract'])
            except Exception as exc:
                print(f'[SYNC] {sym} snapshot inicial error: {exc}')

    with state_lock:
        stale_positions = set(state['positions'].keys()) - current_symbols

    for sym in sorted(stale_positions):
        unsubscribe_symbol(sym, remove_position=True)

    with state_lock:
        state['connected'] = ib.isConnected()
        if current_symbols and not state['last_update']:
            state['last_update'] = time.time()


def on_pending_tickers(tickers):
    now = time.time()
    changed = False

    with state_lock:
        for ticker in tickers:
            sym = getattr(getattr(ticker, 'contract', None), 'symbol', None)
            if not sym or sym not in state['positions']:
                continue

            position = state['positions'][sym]
            price = get_market_price(ticker)
            bid = normalize_price(getattr(ticker, 'bid', None))
            ask = normalize_price(getattr(ticker, 'ask', None))

            if bid == 0:
                bid = price
            if ask == 0:
                ask = price

            if price > 0:
                price_history[sym].append(price)
            apply_price_update(position, price, bid, ask, now)
            changed = True

        state['connected'] = ib.isConnected()
        if changed:
            state['last_update'] = now


async def request_historical_bars(contract):
    return await ib.reqHistoricalDataAsync(
        contract,
        endDateTime='',
        durationStr=HISTORICAL_DURATION,
        barSizeSetting=BAR_SIZE,
        whatToShow='TRADES',
        useRTH=False,
        formatDate=1,
    )


async def request_historical_bars_for_tf(contract, duration, bar_size):
    return await ib.reqHistoricalDataAsync(
        contract,
        endDateTime='',
        durationStr=duration,
        barSizeSetting=bar_size,
        whatToShow='TRADES',
        useRTH=False,
        formatDate=1,
    )


async def request_market_snapshot(contract):
    tickers = await ib.reqTickersAsync(contract)
    return tickers[0] if tickers else None


def refresh_position_snapshot(sym, contract):
    ticker = run_ib_coro(request_market_snapshot(contract))
    if ticker is None:
        price = 0.0
        bid = 0.0
        ask = 0.0
    else:
        price = get_market_price(ticker)
        bid, ask = get_bid_ask(ticker, price)

    try:
        bars = run_ib_coro(request_historical_bars(contract))
    except Exception:
        bars = []

    closes = [bar.close for bar in bars] if bars else []
    if price == 0 and closes:
        price = normalize_price(closes[-1])
        if ticker is not None:
            bid, ask = get_bid_ask(ticker, price)
        else:
            bid, ask = rounded(price), rounded(price)

    with state_lock:
        previous = deepcopy(state['positions'].get(sym, {}))
        recent_prices = list(price_history[sym])

    merged_prices = closes + recent_prices
    if price > 0:
        merged_prices.append(price)

    rsi = calc_rsi(merged_prices) if merged_prices else previous.get('rsi', 50.0)
    macd = calc_macd(merged_prices) if merged_prices else previous.get('macd', {})
    atr = calc_atr(bars) if bars else 0
    volume_metrics = calc_volume_metrics(bars)
    green_consecutive, red_consecutive = consecutive_candles(merged_prices)
    decision, signals = (
        signal_decision(rsi, macd, merged_prices)
        if merged_prices
        else (previous.get('decision', 'WAIT'), previous.get('signals', {}))
    )
    atr_val = atr if atr > 0 else price * 0.005

    with state_lock:
        current = state['positions'].get(sym)
        if not current:
            return
        apply_price_update(current, price, bid, ask)
        current['rsi'] = rsi
        current['rsi_rising'] = len(merged_prices) >= 2 and merged_prices[-1] > merged_prices[-2]
        current['rsi_falling'] = len(merged_prices) >= 2 and merged_prices[-1] < merged_prices[-2]
        current['rsi_data'] = {
            'value': rsi,
            'rising': current['rsi_rising'],
            'falling': current['rsi_falling'],
        }
        current['macd'] = macd
        current['candles'] = {
            'green_consecutive': green_consecutive,
            'red_consecutive': red_consecutive,
        }
        current['atr'] = atr
        current['volume'] = volume_metrics['volume']
        current['avg_volume'] = volume_metrics['avg_volume']
        current['volume_ratio'] = volume_metrics['volume_ratio']
        current['volume_signal'] = volume_metrics['volume_signal']
        current['vol_label'] = volume_label(volume_metrics['volume_ratio'])
        current['sl_suggested'] = round(atr_val * 1.5, 4) if atr_val > 0 else 0.0
        current['tp1_suggested'] = round(atr_val * 2.0, 4) if atr_val > 0 else 0.0
        current['decision'] = decision
        current['signals'] = signals
        state['last_update'] = time.time()


def refresh_multi_tf_analysis(sym, contract):
    multi_tf = {}

    for tf_name, cfg in MULTI_TF_CONFIG.items():
        try:
            bars = run_ib_coro(
                request_historical_bars_for_tf(contract, cfg['duration'], cfg['bar_size'])
            )
        except Exception as exc:
            print(f'[MTF] {sym} {tf_name} error: {exc}')
            continue

        closes = [bar.close for bar in bars] if bars else []
        if not closes:
            continue

        rsi = calc_rsi(closes)
        macd = calc_macd(closes)
        bias = timeframe_bias(rsi, macd, closes)
        volume = calc_volume_metrics(bars)
        multi_tf[tf_name] = {
            'rsi': rsi,
            'macd': macd,
            'bias': bias['bias'],
            'score': bias['score'],
            'summary': bias['summary'],
            'volume_ratio': volume['volume_ratio'],
            'volume_signal': volume['volume_signal'],
            'close': rounded(closes[-1]),
        }

    if not multi_tf:
        return

    style = choose_trade_style(multi_tf)

    with state_lock:
        current = state['positions'].get(sym)
        if not current:
            return
        current['multi_tf'] = multi_tf
        current['trade_style'] = style['trade_style']
        current['trade_note'] = style['trade_note']
        current['structure_note'] = style['structure_note']
        state['last_update'] = time.time()


def analytics_loop():
    while True:
        time.sleep(ANALYTICS_INTERVAL)

        try:
            with state_lock:
                selected_symbol = state.get('selected_symbol', 'ALL')
                active = [
                    (sym, sub['contract'], state['positions'].get(sym, {}).get('price', 0.0))
                    for sym, sub in subscriptions.items()
                    if sym in state['positions'] and sym == selected_symbol
                ]

            for sym, contract, live_price in active:
                try:
                    refresh_position_snapshot(sym, contract)
                    refresh_multi_tf_analysis(sym, contract)
                except FutureTimeoutError:
                    print(f'[ANALYTICS] {sym} timeout refrescando')
                    continue
                except Exception as exc:
                    print(f'[ANALYTICS] {sym} error refrescando: {exc}')
                    continue

        except Exception as exc:
            print(f'[ANALYTICS] loop error: {exc}')


def refresh_all_positions_loop():
    while True:
        time.sleep(ALL_POSITIONS_REFRESH_INTERVAL)

        try:
            with state_lock:
                selected_symbol = state.get('selected_symbol', 'ALL')
                positions_to_refresh = list(state['positions'].keys())

            if selected_symbol != 'ALL' or not positions_to_refresh:
                continue

            refresh_all_positions_once(positions_to_refresh)
        except Exception as exc:
            print(f'[ALL] loop error: {exc}')


def refresh_all_positions_once(symbols=None):
    live_positions = fetch_positions()
    contract_map = {
        pos.contract.symbol: pos.contract
        for pos in live_positions
        if pos.contract.secType == 'STK' and pos.position != 0
    }

    if symbols is None:
        symbols = list(contract_map.keys())

    for sym in symbols:
        try:
            contract = contract_map.get(sym)
            if contract is None:
                continue
            refresh_position_snapshot(sym, contract)
        except Exception as exc:
            print(f'[ALL] {sym} refresh error: {exc}')


def maintenance_loop():
    while True:
        try:
            if not run_on_ib_loop(lambda: ib.isConnected()):
                with state_lock:
                    state['connected'] = False
                print('[IBKR] Conexion perdida')
            else:
                sync_positions()
        except Exception as exc:
            with state_lock:
                state['connected'] = False
            print(f'[MAINT] error: {exc}')

        time.sleep(SYNC_INTERVAL)


# ── HTTP SERVER ────────────────────────────────────────────────

def cors_headers(handler):
    handler.send_header('Access-Control-Allow-Origin', '*')
    handler.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
    handler.send_header('Access-Control-Allow-Headers', 'Content-Type')
    handler.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        cors_headers(self)
        self.end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', len(body))
        cors_headers(self)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/positions':
            self.send_json(snapshot_state())
            return

        if path == '/status':
            with state_lock:
                payload = {
                    'connected': state['connected'],
                    'last_update': state['last_update'],
                    'count': len(state['positions']),
                    'data_mode': state.get('data_mode', market_data_label),
                    'selected_symbol': state.get('selected_symbol', 'ALL'),
                }
            self.send_json(payload)
            return

        if path == '/analysis.json':
            self.send_json(analysis_payload())
            return

        if path.startswith('/focus/'):
            symbol = unquote(path[len('/focus/'):]).strip().upper()
            selected = set_selected_symbol(symbol)
            self.send_json({
                'ok': True,
                'selected_symbol': selected,
            })
            return

        if path.startswith('/position/'):
            symbol = unquote(path[len('/position/'):]).strip().upper()
            position = snapshot_position(symbol)
            if position is None:
                self.send_json({'error': 'symbol not found', 'symbol': symbol}, status=404)
            else:
                self.send_json(position)
            return

        self.send_response(404)
        self.end_headers()


def run_server():
    server = HTTPServer((HOST, PORT), DashboardHandler)
    print(f'[HTTP] Dashboard server en http://{HOST}:{PORT}')
    server.serve_forever()


def start_background_threads():
    global background_threads_started

    if background_threads_started:
        return

    background_threads_started = True
    threading.Thread(target=maintenance_loop, daemon=True).start()
    threading.Thread(target=analytics_loop, daemon=True).start()
    threading.Thread(target=refresh_all_positions_loop, daemon=True).start()


# ── MAIN ───────────────────────────────────────────────────────

if __name__ == '__main__':
    util.patchAsyncio()

    print('=' * 55)
    print('  IBKR Dashboard Server')
    print(f'  TWS:       {TWS_HOST}:{TWS_PORT}  (Live)')
    print(f'  Dashboard: http://localhost:{PORT}')
    print('=' * 55)
    print()
    print('  Tipo de datos de mercado:')
    print('  [1] Tiempo real  (requiere suscripcion en TWS)')
    print('  [2] Delayed 15min (gratis, sin suscripcion)')
    print()

    while True:
        choice = input('  Selecciona [1/2]: ').strip()
        if choice in ('1', '2'):
            break
        print('  Opcion invalida, escribe 1 o 2.')

    market_data_type = 1 if choice == '1' else 3
    market_data_label = 'Tiempo real' if choice == '1' else 'Delayed 15min'
    state['data_mode'] = market_data_label

    connect_ibkr()
    ib.pendingTickersEvent += on_pending_tickers

    threading.Thread(target=run_server, daemon=True).start()

    sync_positions()
    ib_loop.call_soon(start_background_threads)
    ib.run()
