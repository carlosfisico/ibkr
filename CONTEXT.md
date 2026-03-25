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
- `detect_price_spike(bars, multiplier=2.0, period=20)` — rango de vela (`high-low`) vs promedio últimas 20 (captura mechas, no solo cuerpo)
- `detect_volume_spike(bars, multiplier=2.5, period=20)` — volumen vs promedio últimas 20
- `last_spike_times(bars, multiplier_price=2.0, multiplier_vol=2.5, period=20)` — escanea barras de **hoy (NY)** hacia atrás; devuelve `{'last_price_ts': float|None, 'last_vol_ts': float|None}` en Unix seconds. El baseline usa todos los días disponibles para que el promedio sea válido.
- `signal_decision`, `timeframe_bias`, `choose_trade_style`, `session_name` (DST-aware con pytz)

### `ibkr_client.py`
Conexión IBKR, estado global, market data, análisis.
- `BAR_SIZE = '15 mins'`, `HISTORICAL_DURATION = '5 D'`
- `market_data_type = 1` (tiempo real), `market_data_label = 'Tiempo real'`
- `MULTI_TF_CONFIG`: 1m, 5m, 15m, 1h, 4h, 1D
- `_fetch_all_tf_bars` — fetch paralelo con `asyncio.gather`
- `refresh_position_snapshot` — snapshot + RSI/MACD/ATR/SL/TP del TF principal
- `refresh_multi_tf_analysis` — análisis multi-TF + spike detection en barras 15m
- `state['positions'][sym]['spike']` incluye: `price`, `volume`, `last_price_ts`, `last_vol_ts`

### `ibkr_server.py`
HTTP server + threads de fondo + main entry point.
- Arranca directo, sin prompt interactivo (siempre tiempo real)
- Endpoints: `/positions`, `/status`, `/analysis.json`, `/focus/{sym}`, `/position/{sym}`,
  `/config` (devuelve `analytics_interval` + `bar_size`), `/config/analytics/{n}`
- Threads: `maintenance_loop` (reconexión auto), `analytics_loop`, `refresh_all_positions_loop`

### `dashboard.html`
Frontend completo en un solo archivo.
- **Layout 4 columnas**: cards (izq) | grid-a (análisis/teleprompt) | grid-b (contexto multi-TF) | aux-panel (historial, 360px)
  - `grid-template-columns: minmax(340px, 1.6fr) 260px 260px 360px`
- **Privacy toggle** (ojito 👁 en header): `body.privacy-mode .sensitive { visibility:hidden }` — oculta qty, avg cost, P&L no realizado, # posiciones, P&L total
- **Semáforo (traffic light)**: en cada card, lógica jerárquica (no scoring plano):
  - Rojo: sesión ORTH (NY cierre/Asian), rango M15 inválido, entrada tardía, sin dirección
  - Amarillo: señales conflictivas (MACD vs volumen)
  - Verde: todos los filtros pasan
  - Config en objeto `TL` (RSI_LONG_MIN/MAX, VOL_HIGH, CANDLE_TRIGGER, etc.)
  - Estado ORTH: label `'ORTH'`, texto `'Outside regular trading hours'`
- **grid-b — CONTEXTO**: kicker "CONTEXTO" (recordatorio visual de que es solo contexto, no señal operativa)
  - Resumen de alineación arriba del stack: `↑ ALCISTA 4/6` (verde) / `↓ BAJISTA 4/6` (rojo) / `MIXTO 3-3` (amarillo)
  - Conteo sobre TFs disponibles con datos; no aparece si no hay lectura
- **TF selector para RSI/MACD**: botones 1m/5m/15m/1h/4h/1D en settings (default: 15m)
- **Spike 15m** en cada card:
  - Label "Spike 15m" en amarillo bold 13px
  - Cronómetro `"hace Xm Ys"` que persiste entre recargas usando `p.spike.last_price_ts` / `last_vol_ts` del servidor (solo spikes de hoy; muestra `--` si no hubo ninguno en la sesión)
  - `lastSpikeTime[sym]` se inicializa desde el servidor (timestamp más reciente de precio o volumen); fallback a `Date.now()` si hay spike activo sin timestamp
  - `setInterval` de 1s actualiza el DOM del timer sin re-renderizar la card
  - **Alerta sonora**: dos pitidos ascendentes (440Hz→660Hz, Web Audio API) cuando `precio.detected && volumen.detected` pasa de `false` a `true`. Solo suena en la transición, no en cada refresh. `prevBothSpike[sym]` trackea el estado anterior.
- **Countdown badge**: texto "5s", "4s"... en header
- **Settings panel**: refresco dashboard (1s/3s/5s/10s/30s), RSI TF, analytics servidor (1s/3s/5s/10s)
- `esc()` helper anti-XSS en todos los strings del usuario

---

## Lo que sigue (pendiente)

### Spike — mejoras
- [ ] Afinar umbrales: `multiplier` de precio (2.0) y volumen (2.5) — verificar con datos reales en sesión activa
- [ ] En panel central (grid-a/teleprompt), agregar fila "Spike 15m" con descripción textual

### Mejoras de UX pendientes
- [ ] Al seleccionar un ticker, hacer scroll automático a su card
- [ ] Indicar visualmente cuándo los datos de un TF son "frescos" vs. viejos (timestamp de último refresh)
- [ ] En el panel derecho (historial), mostrar el TF de cada entrada guardada (por si el usuario cambia TF a la mitad)

### Posibles features
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
