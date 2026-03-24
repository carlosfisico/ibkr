# IBKR Dashboard — Contexto de desarrollo

## Qué es este proyecto

Dashboard web para monitorear posiciones en Interactive Brokers (IBKR) en tiempo real.
Conecta con TWS (Trader Workstation) vía `ib_insync`. Se corre con `python3 ibkr_server.py`
y se abre `dashboard.html` en el navegador apuntando a `http://localhost:8766`.

## Stack

- Python 3.8.3
- `ib_insync==0.9.70`, `pytz` (ver `requirements.txt`)
- TWS Live en puerto 7496, CLIENT_ID 42
- HTTP server puro (`http.server`), sin Flask ni FastAPI
- Frontend: HTML + JS vanilla, sin frameworks

---

## Arquitectura (3 archivos Python + 1 HTML)

### `indicators.py`
Funciones puras de cálculo, sin estado, sin imports de IBKR.
- `calc_rsi`, `calc_macd` (O(n) con `_ema_series`), `calc_atr`, `calc_volume_metrics`
- `detect_price_spike(bars, multiplier=2.0, period=20)` — cuerpo de vela vs promedio últimas 20
- `detect_volume_spike(bars, multiplier=2.5, period=20)` — volumen vs promedio últimas 20
- `signal_decision`, `timeframe_bias`, `choose_trade_style`, `session_name` (DST-aware con pytz)

### `ibkr_client.py`
Conexión IBKR, estado global, market data, análisis.
- `BAR_SIZE = '15 mins'`, `HISTORICAL_DURATION = '5 D'`
- `market_data_type = 1` (tiempo real), `market_data_label = 'Tiempo real'`
- `MULTI_TF_CONFIG`: 1m, 5m, 15m, 1h, 4h, 1D
- `_fetch_all_tf_bars` — fetch paralelo con `asyncio.gather`
- `refresh_position_snapshot` — snapshot + RSI/MACD/ATR/SL/TP del TF principal
- `refresh_multi_tf_analysis` — análisis multi-TF + **spike detection en barras 15m**
- `state['positions'][sym]` incluye campo `'spike': {'price': {...}, 'volume': {...}}`

### `ibkr_server.py`
HTTP server + threads de fondo + main entry point.
- Arranca directo, sin prompt interactivo (siempre tiempo real)
- Endpoints: `/positions`, `/status`, `/analysis.json`, `/focus/{sym}`, `/position/{sym}`,
  `/config` (devuelve `analytics_interval` + `bar_size`), `/config/analytics/{n}`
- Threads: `maintenance_loop` (reconexión auto), `analytics_loop`, `refresh_all_positions_loop`

### `dashboard.html`
Frontend completo en un solo archivo.
- **TF selector para RSI/MACD**: botones 1m/5m/15m/1h/4h/1D en panel de settings (default: 15m)
- `getRsiModel(p)` y `getMacdModel(p)` leen de `p.multi_tf[rsiTf]` si existe, si no fallback
- **Panel izquierdo** (`side-panel`): teleprompt con tones RSI/MACD/Vol/Decisión + stack multi-TF
- **Panel derecho** (`aux-panel`): historial con timestamps (RSI, MACD, VOL) — cabecera muestra TF activo
- **Spike 15m**: en cada card, dos palomitas independientes: `✓ Precio ×N ↑` y `✓ Vol ×N`
- **Countdown badge**: texto "5s", "4s"... en header, reemplaza la barra de progreso
- **Settings panel**: refresco dashboard (1s/3s/5s/10s/30s), RSI TF, analytics servidor (1s/3s/5s/10s)
- `esc()` helper anti-XSS en todos los strings del usuario

---

## Lo que sigue (pendiente)

### Spike — fase 2 (volumen)
- [ ] Afinar umbrales: `multiplier` de precio (2.0) y volumen (2.5) — verificar con datos reales
- [ ] Considerar usar rango de vela (`high - low`) además del cuerpo (`|close - open|`) para capturar mechas largas
- [ ] En el panel central (teleprompt), agregar una fila de "Spike 15m" con descripción textual igual que RSI/MACD

### Mejoras de UX pendientes
- [ ] Al seleccionar un ticker, hacer scroll automático a su card
- [ ] Indicar visualmente cuándo los datos de un TF son "frescos" vs. viejos (timestamp de último refresh)
- [ ] En el panel derecho (historial), mostrar el TF de cada entrada guardada (por si el usuario cambia TF a la mitad)

### Posibles features
- [ ] Alertas sonoras cuando se detecta spike de precio + volumen simultáneo
- [ ] Exportar historial de señales a CSV
- [ ] Modo "solo señales" que filtre y muestre únicamente posiciones con BUY/SELL activo

---

## Cómo correr

```bash
pip install ib_insync==0.9.70 pytz
# Asegurarse que TWS esté abierto en puerto 7496 con API habilitada
python3 ibkr_server.py
# Abrir dashboard.html en el navegador
```

## Repo
https://github.com/carlosfisico/ibkr
