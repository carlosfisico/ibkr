"""
Conexión a TWS/IB Gateway, streaming de market data,
gestión del estado de posiciones y cálculo de análisis.
"""

import asyncio
import math
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime

from ib_insync import IB, LimitOrder, Ticker, util

import indicators as ind

# ── CONFIG ───────────────────────────────────────────────────────
TWS_HOST            = '127.0.0.1'
TWS_PORT            = 7496
CLIENT_ID           = 42
BAR_SIZE            = '15 mins'
HISTORICAL_DURATION = '5 D'
MULTI_TF_CONFIG     = {
    '1m':  {'duration': '1 D',   'bar_size': '1 min'},
    '5m':  {'duration': '2 D',   'bar_size': '5 mins'},
    '15m': {'duration': '5 D',   'bar_size': '15 mins'},
    '1h':  {'duration': '10 D',  'bar_size': '1 hour'},
    '4h':  {'duration': '90 D',  'bar_size': '4 hours'},
    '1D':  {'duration': '1 Y',   'bar_size': '1 day'},
}
TF_REFRESH_INTERVAL = {   # segundos mínimos entre fetches por TF
    '1m':  60,
    '5m':  60,
    '15m': 60,
    '1h':  900,   # 15 min
    '4h':  3600,  # 60 min
    '1D':  3600,
}
LOOP_TIMEOUT = 20

# ── ESTADO GLOBAL ────────────────────────────────────────────────
ib                = IB()
ib_loop           = None
market_data_type  = 1
market_data_label = 'Tiempo real'
state_lock        = threading.Lock()

state = {
    'connected':      False,
    'positions':      {},
    'last_update':    0,
    'data_mode':      market_data_label,
    'selected_symbol': 'ALL',
}

price_history = defaultdict(lambda: deque(maxlen=200))
subscriptions = {}
bars_cache       = {}  # { (sym, tf): [bars] } — barras cacheadas por símbolo+TF
tf_last_computed = {}  # { (sym, tf): timestamp } — última vez que se hizo fetch+analytics

# ── PACING ────────────────────────────────────────────────────────
pacing_violations = 0
last_violation    = None          # timestamp Unix
recent_requests   = deque()       # timestamps de solicitudes históricas recientes


def _track_request():
    """Registra una solicitud histórica para el contador de pacing."""
    recent_requests.append(time.time())


def requests_last_10s():
    """Retorna cuántas solicitudes históricas se hicieron en los últimos 10s."""
    cutoff = time.time() - 10
    while recent_requests and recent_requests[0] < cutoff:
        recent_requests.popleft()
    return len(recent_requests)


def _on_ib_error(reqId, errorCode, errorString, contract):
    global pacing_violations, last_violation
    if errorCode in (162, 420, 10090):
        pacing_violations += 1
        last_violation = time.time()
        print(f'[PACING] violation {errorCode}: {errorString}')


# ── HELPERS NUMÉRICOS ────────────────────────────────────────────

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
    if bid == 0: bid = price
    if ask == 0: ask = price
    return rounded(bid), rounded(ask)


def rounded(value, digits=4):
    return round(value, digits) if is_valid_number(value) else 0.0


# ── IB EVENT LOOP HELPERS ────────────────────────────────────────

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


# ── GESTIÓN DE ESTADO ────────────────────────────────────────────

def build_position_entry(symbol, qty, avg_cost, contract):
    return {
        'symbol':         symbol,
        'price':          0.0,
        'bid':            0.0,
        'ask':            0.0,
        'qty':            qty,
        'avg_cost':       rounded(avg_cost),
        'pnl_per_share':  0.0,
        'pnl_total':      0.0,
        'pnl_pct':        0.0,
        'rsi_data':       {'value': 50.0, 'rising': False, 'falling': False},
        'macd':           {'line': 0, 'signal': 0, 'hist': 0,
                           'bullish': False, 'cross_up': False, 'cross_down': False},
        'candles':        {'green_consecutive': 0, 'red_consecutive': 0},
        'atr':            0.0,
        'volume':         0,
        'avg_volume':     0,
        'volume_ratio':   0.0,
        'volume_signal':  'LOW',
        'vol_label':      'sin volumen',
        'sl_suggested':   0.0,
        'tp1_suggested':  0.0,
        'sl_price':       0.0,
        'tp1_price':      0.0,
        'spike':          {'price':  {'detected': False, 'ratio': 0.0, 'direction': 'NEUTRAL'},
                           'volume': {'detected': False, 'ratio': 0.0}},
        'decision':       'WAIT',
        'trade_style':    'WAIT',
        'trade_note':     'Sin ventaja clara entre scalp y swing.',
        'structure_note': '15m sin estructura clara',
        'multi_tf':       {},
        'signals':        {},
        'timestamp':      time.time(),
        'session':        ind.session_name(),
        'data_mode':      market_data_label,
        'contract_id':    getattr(contract, 'conId', 0),
    }


def update_position_basics(symbol, qty, avg_cost, contract):
    with state_lock:
        position = state['positions'].get(symbol)
        if position is None:
            position = build_position_entry(symbol, qty, avg_cost, contract)
            state['positions'][symbol] = position
        position['qty']         = qty
        position['avg_cost']    = rounded(avg_cost)
        position['session']     = ind.session_name()
        position['data_mode']   = market_data_label
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
    position['session']   = ind.session_name()
    position['data_mode'] = market_data_label
    recalc_pnl_locked(position)


def recalc_pnl_locked(position):
    price         = normalize_price(position.get('price'))
    avg_cost      = normalize_price(position.get('avg_cost'))
    qty           = position.get('qty', 0)
    pnl_per_share = (price - avg_cost) if price > 0 and avg_cost > 0 else 0.0
    pnl_total     = pnl_per_share * qty
    pnl_pct       = (pnl_per_share / avg_cost * 100) if avg_cost > 0 else 0.0
    position['pnl_per_share'] = rounded(pnl_per_share)
    position['pnl_total']     = round(pnl_total, 2)
    position['pnl_pct']       = round(pnl_pct,   2)


def snapshot_state():
    with state_lock:
        return {
            'connected':      state['connected'],
            'last_update':    state['last_update'],
            'session':        ind.session_name(),
            'positions':      [deepcopy(pos) for pos in state['positions'].values()],
            'timestamp':      time.time(),
            'data_mode':      state.get('data_mode', market_data_label),
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
        position = (deepcopy(state['positions'].get(selected))
                    if selected and selected in state['positions'] else None)

    if not position:
        return {
            'general':   'Sin símbolo seleccionado.',
            'spike':     '',
            'vol_label': 'sin volumen',
            'session':   ind.session_name(),
            'timestamp': time.time(),
        }

    decision    = position.get('decision',       'WAIT')
    trade_style = position.get('trade_style',    'WAIT')
    structure   = position.get('structure_note', '15m sin estructura clara')
    vol         = position.get('vol_label',      'sin volumen')

    if decision == 'BUY':
        general = f"{position['symbol']}: sesgo comprador. {structure}. Mejor para {trade_style.lower()}."
    elif decision == 'SELL':
        general = f"{position['symbol']}: sesgo vendedor. {structure}. Mejor para {trade_style.lower()}."
    elif decision in ('WATCH_BUY', 'WATCH_SELL'):
        general = f"{position['symbol']}: vigilancia activa. {position.get('trade_note', '')}"
    else:
        general = f"{position['symbol']}: sin confirmación clara. {position.get('trade_note', '')}"

    return {
        'general':        general.strip(),
        'spike':          '',
        'vol_label':      vol,
        'session':        position.get('session', ind.session_name()),
        'timestamp':      time.time(),
        'trade_style':    trade_style,
        'decision':       decision,
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
        previous   = state.get('selected_symbol', 'ALL')
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


# ── CONEXIÓN Y SUSCRIPCIONES ─────────────────────────────────────

def connect_ibkr():
    global ib_loop

    print('[IBKR] Conectando a TWS...')
    ib.connect(TWS_HOST, TWS_PORT, clientId=CLIENT_ID)
    ib.reqMarketDataType(market_data_type)
    ib_loop = util.getLoop()

    with state_lock:
        state['connected'] = ib.isConnected()
        state['data_mode'] = market_data_label

    ib.errorEvent += _on_ib_error
    print(f'[IBKR] Conectado | Modo de datos: {market_data_label}')


def subscribe_symbol(sym, qty, avg_cost, contract):
    def subscribe():
        ticker = ib.reqMktData(contract, '', False, False)
        with state_lock:
            subscriptions[sym] = {'contract': contract, 'ticker': ticker}
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
    current   = {}

    for pos in positions:
        contract = pos.contract
        if contract.secType != 'STK':
            continue
        if pos.position == 0:
            continue
        sym = contract.symbol
        current[sym] = {
            'qty':      pos.position,
            'avg_cost': pos.avgCost,
            'contract': contract,
        }

    with state_lock:
        existing_symbols = set(subscriptions.keys())
        selected_symbol  = state.get('selected_symbol', 'ALL')

    current_symbols = set(current.keys())
    active_symbol   = selected_symbol if selected_symbol in current_symbols else None

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


# ── MARKET DATA EVENTS ───────────────────────────────────────────

def on_pending_tickers(tickers):
    now     = time.time()
    changed = False

    with state_lock:
        for ticker in tickers:
            sym = getattr(getattr(ticker, 'contract', None), 'symbol', None)
            if not sym or sym not in state['positions']:
                continue

            position = state['positions'][sym]
            price    = get_market_price(ticker)
            bid      = normalize_price(getattr(ticker, 'bid', None))
            ask      = normalize_price(getattr(ticker, 'ask', None))

            if bid == 0: bid = price
            if ask == 0: ask = price

            if price > 0:
                price_history[sym].append(price)
            apply_price_update(position, price, bid, ask, now)
            changed = True

        state['connected'] = ib.isConnected()
        if changed:
            state['last_update'] = now


# ── DATOS HISTÓRICOS ─────────────────────────────────────────────

async def request_historical_bars(contract):
    _track_request()
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
    _track_request()
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


# ── ANÁLISIS DE POSICIONES ───────────────────────────────────────

def refresh_position_snapshot(sym, contract):
    ticker = run_ib_coro(request_market_snapshot(contract))
    if ticker is None:
        price = bid = ask = 0.0
    else:
        price    = get_market_price(ticker)
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
            bid = ask = rounded(price)

    with state_lock:
        previous      = deepcopy(state['positions'].get(sym, {}))
        recent_prices = list(price_history[sym])

    merged_prices = closes + recent_prices
    if price > 0:
        merged_prices.append(price)

    rsi            = ind.calc_rsi(merged_prices)  if merged_prices else previous.get('rsi', 50.0)
    macd           = ind.calc_macd(merged_prices) if merged_prices else previous.get('macd', {})
    atr            = ind.calc_atr(bars)           if bars else 0
    volume_metrics = ind.calc_volume_metrics(bars)
    green_cons, red_cons = ind.consecutive_candles(merged_prices)
    decision, signals = (
        ind.signal_decision(rsi, macd, merged_prices)
        if merged_prices
        else (previous.get('decision', 'WAIT'), previous.get('signals', {}))
    )
    atr_val = atr if atr > 0 else price * 0.005

    with state_lock:
        current = state['positions'].get(sym)
        if not current:
            return
        apply_price_update(current, price, bid, ask)
        rsi_rising  = len(merged_prices) >= 2 and merged_prices[-1] > merged_prices[-2]
        rsi_falling = len(merged_prices) >= 2 and merged_prices[-1] < merged_prices[-2]
        current['rsi_data']      = {'value': rsi, 'rising': rsi_rising, 'falling': rsi_falling}
        current['macd']          = macd
        current['candles']       = {'green_consecutive': green_cons, 'red_consecutive': red_cons}
        current['atr']           = atr
        current['volume']        = volume_metrics['volume']
        current['avg_volume']    = volume_metrics['avg_volume']
        current['volume_ratio']  = volume_metrics['volume_ratio']
        current['volume_signal'] = volume_metrics['volume_signal']
        current['vol_label']     = ind.volume_label(volume_metrics['volume_ratio'])
        current['sl_suggested']  = round(atr_val * 1.5, 4) if atr_val > 0 else 0.0
        current['tp1_suggested'] = round(atr_val * 2.0, 4) if atr_val > 0 else 0.0
        current['sl_price']      = round(price - atr_val * 1.5, 4) if price > 0 and atr_val > 0 else 0.0
        current['tp1_price']     = round(price + atr_val * 2.0, 4) if price > 0 and atr_val > 0 else 0.0
        current['decision']      = decision
        current['signals']       = signals
        state['last_update']     = time.time()


def _max_bars_for_config(duration_str, bar_size_str):
    """Calcula el número máximo de barras a conservar en caché para un TF."""
    parts = duration_str.split()
    n, unit = int(parts[0]), parts[1].upper()
    trading_days = n * 252 if unit == 'Y' else n

    bs_parts = bar_size_str.split()
    bs_n, bs_unit = int(bs_parts[0]), bs_parts[1].lower()
    if 'min' in bs_unit:
        bar_minutes = bs_n
    elif 'hour' in bs_unit:
        bar_minutes = bs_n * 60
    else:  # day
        bar_minutes = 390

    bars_per_day = max(1, 390 // bar_minutes)
    return int(trading_days * bars_per_day * 1.5)  # 50% buffer


def _merge_bars(cached, new_bars, max_bars):
    """Combina barras cacheadas con nuevas, deduplicando por fecha. Los nuevos ganan."""
    if not new_bars:
        return list(cached)
    if not cached:
        result = list(new_bars)
        return result[-max_bars:] if max_bars and len(result) > max_bars else result

    bar_dict = {str(b.date): b for b in cached}
    for b in new_bars:
        bar_dict[str(b.date)] = b

    merged = sorted(bar_dict.values(), key=lambda b: str(b.date))
    if max_bars and len(merged) > max_bars:
        merged = merged[-max_bars:]
    return merged


def _delta_duration(last_bar_date):
    """
    Calcula la duración a pedir desde la última barra hasta ahora.
    Retorna string IBKR ('N S' o 'N D'), o None si no hay nada nuevo.
    """
    date_str = str(last_bar_date).strip()
    try:
        if ' ' in date_str:
            dt = datetime.strptime(date_str.replace('  ', ' '), '%Y%m%d %H:%M:%S')
        else:
            dt = datetime.strptime(date_str, '%Y%m%d')
    except ValueError:
        return None

    delta = (datetime.now() - dt).total_seconds()
    if delta < 60:
        return None  # sin barras nuevas

    with_margin = delta * 1.2
    if with_margin <= 86400:
        return f'{int(with_margin)} S'
    return f'{math.ceil(with_margin / 86400)} D'


async def _fetch_all_tf_bars(contract, sym):
    now              = time.time()
    tasks            = []   # coroutine, None (cache/delta hit), or 'skip' (within interval)
    within_interval  = set()

    for tf_name, cfg in MULTI_TF_CONFIG.items():
        interval = TF_REFRESH_INTERVAL.get(tf_name, 60)
        if now - tf_last_computed.get((sym, tf_name), 0) < interval:
            within_interval.add(tf_name)
            tasks.append(None)
            continue

        cached = bars_cache.get((sym, tf_name))
        if cached:
            duration = _delta_duration(cached[-1].date)
            if duration is None:
                tasks.append(None)
            else:
                print(f'[MTF] {sym} {tf_name} delta {duration}')
                tasks.append(request_historical_bars_for_tf(contract, duration, cfg['bar_size']))
        else:
            print(f'[MTF] {sym} {tf_name} fetch completo')
            tasks.append(request_historical_bars_for_tf(contract, cfg['duration'], cfg['bar_size']))

    real_coros   = [t for t in tasks if t is not None]
    real_results = await asyncio.gather(*real_coros, return_exceptions=True) if real_coros else []

    results  = []
    real_idx = 0
    for tf_name, cfg in MULTI_TF_CONFIG.items():
        if tf_name in within_interval:
            results.append(None)  # señal: dentro del intervalo, skip analytics
            continue

        task = tasks[len(results)]
        if task is None:
            # delta=0, sin barras nuevas; procesar caché
            tf_last_computed[(sym, tf_name)] = now
            results.append(list(bars_cache.get((sym, tf_name), [])))
        else:
            result = real_results[real_idx]
            real_idx += 1
            tf_last_computed[(sym, tf_name)] = now
            if isinstance(result, Exception):
                cached = bars_cache.get((sym, tf_name))
                if cached:
                    print(f'[MTF] {sym} {tf_name} error pacing, usando caché: {result}')
                    results.append(list(cached))
                else:
                    results.append(result)
            else:
                cached   = bars_cache.get((sym, tf_name), [])
                max_bars = _max_bars_for_config(cfg['duration'], cfg['bar_size'])
                merged   = _merge_bars(cached, result, max_bars)
                bars_cache[(sym, tf_name)] = merged
                results.append(list(merged))

    return results


def refresh_multi_tf_analysis(sym, contract):
    tf_names = list(MULTI_TF_CONFIG.keys())
    results  = run_ib_coro(_fetch_all_tf_bars(contract, sym))

    # Preservar datos existentes de TFs dentro de su intervalo
    with state_lock:
        pos      = state['positions'].get(sym)
        multi_tf = deepcopy(pos.get('multi_tf', {})) if pos else {}

    for tf_name, result in zip(tf_names, results):
        if result is None:
            continue  # dentro del intervalo, conservar datos anteriores
        if isinstance(result, Exception):
            print(f'[MTF] {sym} {tf_name} error: {result}')
            continue

        bars   = result
        closes = [bar.close for bar in bars] if bars else []
        if not closes:
            continue

        rsi    = ind.calc_rsi(closes)
        macd   = ind.calc_macd(closes)
        bias   = ind.timeframe_bias(rsi, macd, closes)
        volume = ind.calc_volume_metrics(bars)
        multi_tf[tf_name] = {
            'rsi':           rsi,
            'macd':          macd,
            'bias':          bias['bias'],
            'score':         bias['score'],
            'summary':       bias['summary'],
            'volume_ratio':  volume['volume_ratio'],
            'volume_signal': volume['volume_signal'],
            'close':         rounded(closes[-1]),
            'last_updated':  tf_last_computed.get((sym, tf_name), time.time()),
        }

    if not multi_tf:
        return

    style = ind.choose_trade_style(multi_tf)

    # Spike detection siempre desde el caché de 15m (independiente del intervalo)
    bars_15m = bars_cache.get((sym, '15m'), [])
    _empty_times = {'last_price_ts': None, 'last_vol_ts': None,
                    'last_price_range': None, 'last_price_avg': None, 'last_price_close': None,
                    'last_vol_amount': None, 'last_vol_avg': None}
    spike_times = ind.last_spike_times(bars_15m) if bars_15m else _empty_times
    spike = {
        'price':           ind.detect_price_spike(bars_15m)  if bars_15m else {'detected': False, 'ratio': 0.0, 'direction': 'NEUTRAL', 'current_range': 0.0, 'avg_range': 0.0},
        'volume':          ind.detect_volume_spike(bars_15m) if bars_15m else {'detected': False, 'ratio': 0.0, 'current_vol': 0, 'avg_vol': 0},
        'last_price_ts':    spike_times['last_price_ts'],
        'last_vol_ts':      spike_times['last_vol_ts'],
        'last_price_range': spike_times['last_price_range'],
        'last_price_avg':   spike_times['last_price_avg'],
        'last_price_close': spike_times['last_price_close'],
        'last_vol_amount':  spike_times['last_vol_amount'],
        'last_vol_avg':     spike_times['last_vol_avg'],
    }

    with state_lock:
        current = state['positions'].get(sym)
        if not current:
            return
        current['multi_tf']       = multi_tf
        current['spike']          = spike
        current['trade_style']    = style['trade_style']
        current['trade_note']     = style['trade_note']
        current['structure_note'] = style['structure_note']
        state['last_update']      = time.time()


def refresh_all_positions_once(symbols=None):
    live_positions = fetch_positions()
    contract_map   = {
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


# ── ÓRDENES ──────────────────────────────────────────────────────

async def _place_order_async(contract, order):
    trade = ib.placeOrder(contract, order)
    await asyncio.sleep(2.0)   # dar tiempo a TWS para procesar y responder
    return trade


def place_order(sym, action, qty, limit_price):
    """Coloca una limit order DAY. action='BUY'|'SELL'."""
    with state_lock:
        sub = subscriptions.get(sym)

    if not sub:
        raise ValueError(f'{sym} no tiene suscripción activa')

    contract    = sub['contract']
    limit_price = round(float(limit_price), 2)
    order       = LimitOrder(action, int(qty), limit_price)
    order.tif   = 'DAY'   # evita error 10349 por preset de TWS

    trade  = run_ib_coro(_place_order_async(contract, order))
    status = trade.orderStatus.status
    print(f'[ORDER] {action} {qty} {sym} @ {limit_price} → orderId={trade.order.orderId} status={status}')

    if status in ('Cancelled', 'Inactive'):
        err_msgs = [e.message for e in trade.log if e.message and e.errorCode > 0]
        raise ValueError(err_msgs[-1] if err_msgs else f'Orden cancelada por TWS (status: {status})')

    return {
        'ok':          True,
        'order_id':    trade.order.orderId,
        'status':      status,
        'sym':         sym,
        'action':      action,
        'qty':         int(qty),
        'limit_price': limit_price,
    }
