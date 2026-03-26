"""
IBKR Dashboard Server — punto de entrada principal.
Levanta el servidor HTTP, coordina los threads de fondo
y conecta con TWS Live (puerto 7496).

Uso:
    pip install ib_insync==0.9.70
    python3 ibkr_server.py
"""

import json
import os
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import unquote

from ib_insync import util

import ibkr_client as client

# ── CONFIG HTTP ──────────────────────────────────────────────────
HOST                          = '127.0.0.1'
PORT                          = 8766
FOCUS_FILE                    = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'focused_symbol.txt')
SYNC_INTERVAL                 = 3
ANALYTICS_INTERVAL            = 3
ALL_POSITIONS_REFRESH_INTERVAL = 30

background_threads_started = False
server_config = {'analytics_interval': ANALYTICS_INTERVAL, 'paused': False}


# ── HTTP SERVER ──────────────────────────────────────────────────

def cors_headers(handler):
    handler.send_header('Access-Control-Allow-Origin',  '*')
    handler.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    handler.send_header('Access-Control-Allow-Headers', 'Content-Type')
    handler.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(200)
        cors_headers(self)
        self.end_headers()

    def do_POST(self):
        path = self.path.split('?')[0]

        if path == '/order':
            length = int(self.headers.get('Content-Length', 0))
            try:
                data        = json.loads(self.rfile.read(length))
                sym         = str(data.get('sym', '')).strip().upper()
                action      = str(data.get('action', '')).strip().upper()
                qty         = int(data.get('qty', 0))
                limit_price = float(data.get('limit_price', 0))

                if not sym or action not in ('BUY', 'SELL') or qty <= 0 or limit_price <= 0:
                    self.send_json({'error': 'parámetros inválidos'}, status=400)
                    return

                result = client.place_order(sym, action, qty, limit_price)
                self.send_json(result)
            except Exception as exc:
                self.send_json({'error': str(exc)}, status=500)
            return

        self.send_response(404)
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
            self.send_json(client.snapshot_state())
            return

        if path == '/status':
            with client.state_lock:
                payload = {
                    'connected':          client.state['connected'],
                    'last_update':        client.state['last_update'],
                    'count':              len(client.state['positions']),
                    'data_mode':          client.state.get('data_mode', client.market_data_label),
                    'selected_symbol':    client.state.get('selected_symbol', 'ALL'),
                    'requests_last_10s':  client.requests_last_10s(),
                    'pacing_violations':  client.pacing_violations,
                    'last_violation':     client.last_violation,
                }
            self.send_json(payload)
            return

        if path == '/analysis.json':
            self.send_json(client.analysis_payload())
            return

        if path.startswith('/focus/'):
            symbol   = unquote(path[len('/focus/'):]).strip().upper()
            selected = client.set_selected_symbol(symbol)
            if selected and selected != 'ALL':
                with open(FOCUS_FILE, 'w') as f:
                    f.write(selected)
            else:
                try:
                    os.remove(FOCUS_FILE)
                except FileNotFoundError:
                    pass
            self.send_json({'ok': True, 'selected_symbol': selected})
            return

        if path.startswith('/position/'):
            symbol   = unquote(path[len('/position/'):]).strip().upper()
            position = client.snapshot_position(symbol)
            if position is None:
                self.send_json({'error': 'symbol not found', 'symbol': symbol}, status=404)
            else:
                self.send_json(position)
            return

        if path == '/pause':
            server_config['paused'] = not server_config['paused']
            state = 'paused' if server_config['paused'] else 'running'
            print(f'[SERVER] {state}')
            self.send_json({'ok': True, 'paused': server_config['paused']})
            return

        if path == '/config':
            self.send_json({'analytics_interval': server_config['analytics_interval'],
                            'bar_size': client.BAR_SIZE, 'paused': server_config['paused']})
            return

        if path.startswith('/config/analytics/'):
            try:
                seconds = int(unquote(path[len('/config/analytics/'):]))
                if 1 <= seconds <= 60:
                    server_config['analytics_interval'] = seconds
                    self.send_json({'ok': True, 'analytics_interval': seconds})
                else:
                    self.send_json({'error': 'rango valido: 1-60'}, status=400)
            except ValueError:
                self.send_json({'error': 'valor invalido'}, status=400)
            return

        self.send_response(404)
        self.end_headers()


def run_server():
    server = HTTPServer((HOST, PORT), DashboardHandler)
    print(f'[HTTP] Dashboard server en http://{HOST}:{PORT}')
    server.serve_forever()


# ── BACKGROUND THREADS ───────────────────────────────────────────

def analytics_loop():
    all_ticks = 0
    ALL_REFRESH_EVERY = 5  # cada 5 ticks × 3s = 15s en modo ALL

    while True:
        time.sleep(server_config['analytics_interval'])
        if server_config['paused']:
            continue
        try:
            with client.state_lock:
                selected_symbol = client.state.get('selected_symbol', 'ALL')
                active = [
                    (sym, sub['contract'])
                    for sym, sub in client.subscriptions.items()
                    if sym in client.state['positions'] and sym == selected_symbol
                ]

            if selected_symbol == 'ALL':
                all_ticks += 1
                if all_ticks >= ALL_REFRESH_EVERY:
                    all_ticks = 0
                    client.refresh_all_positions_once()
                continue

            all_ticks = 0
            for sym, contract in active:
                try:
                    client.refresh_position_snapshot(sym, contract)
                    client.refresh_multi_tf_analysis(sym, contract)
                except FutureTimeoutError:
                    print(f'[ANALYTICS] {sym} timeout refrescando')
                except Exception as exc:
                    print(f'[ANALYTICS] {sym} error refrescando: {exc}')

        except Exception as exc:
            print(f'[ANALYTICS] loop error: {exc}')


def refresh_all_positions_loop():
    while True:
        time.sleep(ALL_POSITIONS_REFRESH_INTERVAL)
        if server_config['paused']:
            continue
        try:
            with client.state_lock:
                selected_symbol      = client.state.get('selected_symbol', 'ALL')
                positions_to_refresh = list(client.state['positions'].keys())

            if selected_symbol != 'ALL' or not positions_to_refresh:
                continue

            client.refresh_all_positions_once(positions_to_refresh)
        except Exception as exc:
            print(f'[ALL] loop error: {exc}')


def maintenance_loop():
    while True:
        try:
            if not client.run_on_ib_loop(lambda: client.ib.isConnected()):
                with client.state_lock:
                    client.state['connected'] = False
                print('[IBKR] Conexion perdida — reconectando...')
                try:
                    client.connect_ibkr()
                    client.ib.pendingTickersEvent += client.on_pending_tickers
                    client.sync_positions()
                    print('[IBKR] Reconectado')
                except Exception as e:
                    print(f'[IBKR] Reconexion fallida: {e}')
            else:
                client.sync_positions()
        except Exception as exc:
            with client.state_lock:
                client.state['connected'] = False
            print(f'[MAINT] error: {exc}')

        time.sleep(SYNC_INTERVAL)


def start_background_threads():
    global background_threads_started
    if background_threads_started:
        return
    background_threads_started = True
    threading.Thread(target=maintenance_loop,           daemon=True).start()
    threading.Thread(target=analytics_loop,             daemon=True).start()
    threading.Thread(target=refresh_all_positions_loop, daemon=True).start()


# ── MAIN ─────────────────────────────────────────────────────────

if __name__ == '__main__':
    util.patchAsyncio()

    print('=' * 55)
    print('  IBKR Dashboard Server')
    print(f'  TWS:       {client.TWS_HOST}:{client.TWS_PORT}  (Live)')
    print(f'  Dashboard: http://localhost:{PORT}')
    print(f'  Datos:     {client.market_data_label}')
    print('=' * 55)

    if os.path.exists(FOCUS_FILE):
        sym = open(FOCUS_FILE).read().strip().upper()
        if sym and sym != 'ALL':
            with client.state_lock:
                client.state['selected_symbol'] = sym
            print(f'[FOCUS] símbolo restaurado desde disco: {sym}')

    client.connect_ibkr()
    client.ib.pendingTickersEvent += client.on_pending_tickers

    threading.Thread(target=run_server, daemon=True).start()

    client.sync_positions()
    client.ib_loop.call_soon(start_background_threads)
    client.ib.run()
