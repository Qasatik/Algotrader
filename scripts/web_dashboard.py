#!/usr/bin/env python3
"""Read-only web dashboard for the carry bot — positions, balance, charts.

Runs a tiny Flask app (localhost by default):

* bot service status (systemd) + last journal heartbeat
* balance cards: total equity, free USDT, coins
* open positions table (perp shorts): size, notional, uPnL, liq
* funding scanner: 14-symbol universe ranked by funding
* CHARTS (Chart.js, vendored locally in static/ — no CDN):
    - equity curve (line) with day/week/month bucketing
    - PnL per bucket (green/red bars)
    - funding ranking (horizontal bars)
    - position notional distribution (doughnut)
* metrics: total return, max drawdown, buckets count
* recent trades from data/carry_trades.csv

Best practices applied:
* READ-ONLY — the dashboard never places orders
* server-side aggregation (day/week/month) — thin client
* smooth polling via fetch() (no full-page reload flicker),
  <noscript> meta-refresh fallback
* Chart.js served from local static/ — works offline
* responsive dark theme, tabular numbers, formatted tooltips

Usage:
    PYTHONPATH=. python3 scripts/web_dashboard.py                 # 127.0.0.1:8500
    PYTHONPATH=. python3 scripts/web_dashboard.py --port 8600
    PYTHONPATH=. python3 scripts/web_dashboard.py --host 0.0.0.0  # LAN (careful!)

Endpoints:
    /             HTML dashboard (JS polls /api/status)
    /api/status   everything as JSON (incl. bucketed equity/pnl series)
    /static/...   vendored Chart.js
"""
from __future__ import annotations

import argparse
import csv
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request

from core.exchange import BybitExchange

# Same universe as scripts/run_carry_multi.py:SCAN_UNIVERSE (copied to avoid
# importing the runner module, which parses CLI args at import time).
UNIVERSE = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "AVAXUSDT", "LINKUSDT", "ADAUSDT", "BNBUSDT",
    "OPUSDT", "ARBUSDT", "SUIUSDT", "APTUSDT", "NEARUSDT",
]

TRADES_CSV = Path("data/carry_trades.csv")
PNL_CSV = Path("data/carry_pnl.csv")
MAX_POINTS = 1000  # downsample cap per series

# static_folder must be absolute: when run as "python3 scripts/web_dashboard.py",
# Flask's auto-detected root path is scripts/, not the project root.
app = Flask(__name__, static_folder=str(Path(__file__).resolve().parent.parent / "static"))
_ex: BybitExchange | None = None

_cache: dict = {"scan": None, "scan_ts": 0.0, "svc": None, "svc_ts": 0.0,
                "pnl": None, "pnl_ts": 0.0}
SCAN_TTL = 60.0
SVC_TTL = 10.0
PNL_TTL = 120.0


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------- aggregation --
def _bucket_key(ts_iso: str, bucket: str) -> str:
    """Group a timestamp into day / week (ISO) / month key."""
    try:
        t = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return ts_iso[:10]
    if bucket == "month":
        return f"{t:%Y-%m}"
    if bucket == "week":
        y, w, _ = t.isocalendar()
        return f"{y}-W{w:02d}"
    return f"{t:%Y-%m-%d}"


def _equity_stats(vals: list[float]) -> dict:
    """Total return + max drawdown over an equity level series."""
    if len(vals) < 2:
        return {"ret": 0.0, "ret_pct": 0.0, "maxdd": 0.0, "maxdd_pct": 0.0}
    ret = vals[-1] - vals[0]
    ret_pct = ret / vals[0] * 100.0 if vals[0] else 0.0
    peak, maxdd, maxdd_pct = vals[0], 0.0, 0.0
    for v in vals:
        peak = max(peak, v)
        dd = peak - v
        if dd > maxdd:
            maxdd, maxdd_pct = dd, dd / peak * 100.0 if peak else 0.0
    return {"ret": round(ret, 4), "ret_pct": round(ret_pct, 2),
            "maxdd": round(maxdd, 4), "maxdd_pct": round(maxdd_pct, 2)}


def _equity_series(bucket: str, live_equity: float | None = None) -> dict:
    """Bucketed equity levels + per-bucket PnL deltas + stats (cached).

    The CSV only gains a row every bot heartbeat (~2h), so the freshest LIVE
    reading is merged in as the newest point — the chart never shows stale
    data just because the next snapshot hasn't fired yet (2026-08-22: the
    chart was stuck on July data for a month while the account traded).
    """
    now = time.time()
    if _cache["pnl"] is None or now - _cache["pnl_ts"] > PNL_TTL:
        rows: list[dict] = []
        if PNL_CSV.exists():
            try:
                with PNL_CSV.open() as f:
                    rows = [
                        {"ts": r.get("timestamp", ""),
                         "eq": _f(r.get("equity_usdt"))}
                        for r in csv.DictReader(f) if _f(r.get("equity_usdt")) > 0
                    ]
            except Exception:
                rows = []
        _cache["pnl"] = rows
        _cache["pnl_ts"] = now
    rows = _cache["pnl"]

    levels: dict[str, float] = {}
    for r in rows:                       # last equity per bucket wins
        levels[_bucket_key(r["ts"], bucket)] = r["eq"]
    # Merge the live reading so "сейчас" is always the newest chart point.
    if live_equity and live_equity > 0:
        levels[_bucket_key(datetime.now(timezone.utc).isoformat(), bucket)] = live_equity
    keys = sorted(levels)
    if len(keys) > MAX_POINTS:
        keys = keys[-MAX_POINTS:]
    vals = [round(levels[k], 4) for k in keys]

    pnl_labels, pnl_vals = [], []
    for i in range(1, len(keys)):
        pnl_labels.append(keys[i])
        pnl_vals.append(round(vals[i] - vals[i - 1], 4))

    stats = _equity_stats(vals)
    # Observed span: first snapshot → now (live point included).
    span_days = 0.0
    if rows:
        try:
            t0 = datetime.fromisoformat(rows[0]["ts"].replace("Z", "+00:00"))
            span_days = max(
                (datetime.now(timezone.utc) - t0).total_seconds() / 86400.0, 0.0)
        except ValueError:
            pass
    # APR: annualised return — only meaningful once ≥1 day is observed.
    apr = stats["ret_pct"] / span_days * 365.0 if span_days >= 1.0 else None
    return {"labels": keys, "equity": vals,
            "pnl_labels": pnl_labels, "pnl_values": pnl_vals,
            "stats": {**stats,
                      "apr_pct": round(apr, 1) if apr is not None else None,
                      "span_days": round(span_days, 1)},
            "snapshots": len(rows), "buckets": len(keys),
            "has_data": len(keys) >= 2}


# ------------------------------------------------------------- collectors --
def _service_status() -> dict:
    now = time.time()
    if _cache["svc"] is not None and now - _cache["svc_ts"] < SVC_TTL:
        return _cache["svc"]
    info = {"active": False, "uptime": "?", "heartbeat": "", "heartbeat_age": None}
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", "carry-bot"],
            capture_output=True, text=True, timeout=5)
        info["active"] = r.stdout.strip() == "active"
        if info["active"]:
            r = subprocess.run(
                ["systemctl", "--user", "show", "carry-bot",
                 "--property=ActiveEnterTimestamp", "--value"],
                capture_output=True, text=True, timeout=5)
            ts = r.stdout.strip()
            info["uptime"] = ts
            try:
                t0 = datetime.strptime(ts, "%a %Y-%m-%d %H:%M:%S %Z")
                info["uptime"] = str(datetime.now() - t0).split(".")[0]
            except ValueError:
                pass
        r = subprocess.run(
            ["journalctl", "--user", "-u", "carry-bot", "-n", "1",
             "--output=short-iso", "--no-pager"],
            capture_output=True, text=True, timeout=5)
        parts = r.stdout.strip().split(" ", 1)
        if parts and parts[0]:
            try:
                t = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
                info["heartbeat_age"] = round(
                    (datetime.now(timezone.utc) - t).total_seconds(), 0)
            except ValueError:
                pass
        r = subprocess.run(
            ["journalctl", "--user", "-u", "carry-bot", "-n", "1",
             "--output=cat", "--no-pager"],
            capture_output=True, text=True, timeout=5)
        info["heartbeat"] = r.stdout.strip()[:160]
    except Exception:
        pass
    _cache["svc"] = info
    _cache["svc_ts"] = now
    return info


def _funding_scan() -> list[dict]:
    now = time.time()
    if _cache["scan"] is not None and now - _cache["scan_ts"] < SCAN_TTL:
        return _cache["scan"]
    rows: list[dict] = []
    for sym in UNIVERSE:
        try:
            fr = _ex.get_funding_rate(sym) or {}
            rate = _f(fr.get("fundingRate"))
            mark = _f(fr.get("markPrice"))
            spot = _ex.get_spot_price(sym) or 0.0
            basis = (mark - spot) / spot * 10_000.0 if spot > 0 else 0.0
            rows.append({"symbol": sym, "funding": rate,
                         "funding_pct": rate * 100.0,
                         "basis_bps": round(basis, 1)})
        except Exception:
            rows.append({"symbol": sym, "funding": 0.0,
                         "funding_pct": 0.0, "basis_bps": 0.0})
    rows.sort(key=lambda r: r["funding"], reverse=True)
    _cache["scan"] = rows
    _cache["scan_ts"] = now
    return rows


def _balance() -> dict:
    """Equity + available margin + unrealised PnL + per-coin rows."""
    total, coins = _ex.get_total_equity()
    available = 0.0
    try:
        res = _ex.get_wallet_balance("USDT")
        available = _f(res["list"][0].get("totalAvailableBalance"))
    except Exception:
        pass
    wallet_usd = sum(c["usd_value"] for c in coins)
    return {"total_equity": round(total, 4),
            "available": round(available, 4),
            "upnl": round(total - wallet_usd, 4),
            "coins": [{"coin": c["coin"],
                       "wallet": round(c["wallet_balance"], 6),
                       "usd": round(c["usd_value"], 4)} for c in coins]}


def _positions(scan: list[dict]) -> list[dict]:
    fmap = {r["symbol"]: r for r in scan}
    out: list[dict] = []
    try:
        for p in _ex.get_positions():
            if _f(p.get("size")) <= 0:
                continue
            sym = p.get("symbol", "?")
            out.append({
                "symbol": sym, "side": p.get("side", "?"),
                "size": _f(p.get("size")),
                "notional": round(_f(p.get("positionValue")), 2),
                "entry": _f(p.get("avgPrice")),
                "mark": _f(p.get("markPrice")),
                "upnl": round(_f(p.get("unrealisedPnl")), 4),
                "rpnl_cum": round(_f(p.get("cumRealisedPnl")), 4),
                "liq": _f(p.get("liqPrice")),
                "funding_pct": fmap.get(sym, {}).get("funding_pct", 0.0),
                "basis_bps": fmap.get(sym, {}).get("basis_bps", 0.0),
            })
    except Exception:
        pass
    return out


def _recent_trades(n: int = 12) -> list[dict]:
    if not TRADES_CSV.exists():
        return []
    try:
        with TRADES_CSV.open() as f:
            rows = list(csv.DictReader(f))
        # Defensive: rows can carry MORE fields than the header (the trade
        # log gained a "confidence" column without a header bump) — the
        # overflow lands under DictReader's restkey=None, which breaks
        # jsonify(sort_keys=True). Drop non-str keys and normalise.
        clean = []
        for r in rows:
            if not r.get("timestamp"):
                continue
            clean.append({k: v for k, v in r.items() if isinstance(k, str)})
        clean.reverse()
        return clean[:n]
    except Exception:
        return []


def collect(bucket: str = "day") -> dict:
    scan = _funding_scan()
    bal = _balance()
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "service": _service_status(),
        "balance": bal,
        "positions": _positions(scan),
        "scan": scan,
        "trades": _recent_trades(),
        "equity": _equity_series(bucket, live_equity=bal["total_equity"]),
    }


# ------------------------------------------------------------------ views --
CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:#0d1117;color:#c9d1d9;
     padding:18px;max-width:1200px;margin:0 auto}
h1{font-size:1.25rem;margin-bottom:4px;color:#58a6ff}
.sub{color:#8b949e;font-size:.8rem;margin-bottom:16px}
h2{font-size:.95rem;color:#79c0ff;margin:18px 0 8px;border-bottom:1px solid #21262d;
   padding-bottom:4px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.card{background:#161b22;border:1px solid #21262d;border-radius:8px;
      padding:10px 16px;min-width:150px}
.card .lbl{font-size:.7rem;color:#8b949e;text-transform:uppercase;letter-spacing:.5px}
.card .val{font-size:1.25rem;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
.ok{color:#3fb950}.bad{color:#f85149}.warn{color:#d29922}.muted{color:#8b949e}
table{width:100%;border-collapse:collapse;font-size:.8rem;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:5px 8px;border-bottom:1px solid #21262d}
th{color:#8b949e;font-weight:500;font-size:.72rem;text-transform:uppercase}
td:first-child,th:first-child{text-align:left}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:.68rem}
.tag.short{background:#3d2020;color:#f85149}
.star{color:#e3b341}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:900px){.grid2{grid-template-columns:1fr}}
.chart-box{position:relative;height:260px;background:#161b22;
           border:1px solid #21262d;border-radius:8px;padding:10px}
.chart-box.tall{height:340px}
.toggle{display:inline-block;margin-left:10px;font-size:.75rem}
.toggle a{color:#8b949e;text-decoration:none;padding:2px 8px;border-radius:6px}
.toggle a.on{background:#1f6feb;color:#fff}
.toggle a:hover{color:#c9d1d9}
footer{margin-top:22px;color:#484f58;font-size:.7rem;text-align:center}
"""

JS = r"""
const FMT = v => (v==null?'—':Number(v).toLocaleString('ru-RU',
  {maximumFractionDigits:4}));
const FMT2 = v => (v==null?'—':Number(v).toLocaleString('ru-RU',
  {maximumFractionDigits:2}));
const GRID='#21262d', TICK='#8b949e';
Chart.defaults.color=TICK; Chart.defaults.borderColor=GRID;
Chart.defaults.font.family="'Segoe UI',system-ui,sans-serif";
Chart.defaults.plugins.legend.labels.boxWidth=12;

let charts={};
function mk(id,cfg){ if(charts[id]) charts[id].destroy(); 
  const el=document.getElementById(id); if(!el) return;
  charts[id]=new Chart(el,cfg); }

function esc(s){const d=document.createElement('div');d.textContent=s??'';return d.innerHTML;}

function render(d){
  // header cards
  const svc=d.service, bal=d.balance, eq=d.equity, st=eq.stats;
  document.getElementById('svc').innerHTML = svc.active
    ? '<span class="ok">● RUNNING</span>' : '<span class="bad">● STOPPED</span>';
  document.getElementById('uptime').textContent='uptime '+svc.uptime;
  document.getElementById('eq').innerHTML=FMT2(bal.total_equity)
    +' <span class="muted" style="font-size:.8rem">USDT</span>';
  document.getElementById('avail').textContent='доступно '+FMT2(bal.available)
    +' · uPnL '+(bal.upnl>=0?'+':'')+FMT2(bal.upnl);
  const notl=d.positions.reduce((s,p)=>s+(p.notional||0),0);
  document.getElementById('npos').textContent=d.positions.length;
  document.getElementById('npossub').textContent='нотионал $'+FMT2(notl);
  const hb=svc.heartbeat_age;
  const hbEl=document.getElementById('hb');
  hbEl.textContent = hb==null?'?':(hb<60?hb.toFixed(0)+' с назад'
    :hb<3600?(hb/60).toFixed(0)+' мин назад':(hb/3600).toFixed(1)+' ч назад');
  hbEl.className='val '+(hb!=null&&hb<120?'ok':'warn');
  hbEl.style.fontSize='1rem';
  document.getElementById('hbt').textContent=svc.heartbeat.slice(0,90);
  document.getElementById('coins').textContent=bal.coins.map(c=>
    c.coin+' '+FMT(c.wallet)+' ($'+FMT2(c.usd)+')').join('  ·  ');
  document.getElementById('ts').textContent=d.ts+' UTC';

  // positions table
  const pos=document.getElementById('posbody');
  pos.innerHTML = d.positions.length ? d.positions.map(p=>
    `<tr><td>${p.symbol}</td><td><span class="tag short">${p.side}</span></td>`+
    `<td>${FMT(p.size)}</td><td>${FMT2(p.notional)}</td>`+
    `<td>${FMT(p.entry)}</td><td>${FMT(p.mark)}</td>`+
    `<td class="${p.upnl>=0?'ok':'bad'}">${(p.upnl>=0?'+':'')+FMT(p.upnl)}</td>`+
    `<td class="${p.rpnl_cum>=0?'ok':'bad'}">${(p.rpnl_cum>=0?'+':'')+FMT(p.rpnl_cum)}</td>`+
    `<td>${(p.funding_pct>=0?'+':'')+p.funding_pct.toFixed(4)}%</td>`+
    `<td>${(p.basis_bps>=0?'+':'')+p.basis_bps.toFixed(1)}</td>`+
    `<td class="muted">${FMT(p.liq)}</td></tr>`).join('')
    : '<tr><td colspan="11" class="muted">открытых позиций нет (FLAT)</td></tr>';

  // funding table
  document.getElementById('scanbody').innerHTML=d.scan.map((r,i)=>
    `<tr><td>${i<5?'<span class="star">★</span> ':''}${r.symbol}</td>`+
    `<td class="${r.funding>=0?'ok':'bad'}">`+
    `${(r.funding_pct>=0?'+':'')+r.funding_pct.toFixed(4)}%</td>`+
    `<td>${(r.funding_pct*3*365>=0?'+':'')+(r.funding_pct*3*365).toFixed(1)}%</td>`+
    `<td>${(r.basis_bps>=0?'+':'')+r.basis_bps.toFixed(1)}</td></tr>`).join('');

  // trades table
  document.getElementById('trbody').innerHTML = d.trades.length
    ? d.trades.map(t=>`<tr><td>${(t.timestamp||'').slice(0,19)}</td>`+
      `<td>${t.action||''}</td><td>${t.symbol||''}</td><td>${t.side||''}</td>`+
      `<td>${t.qty||''}</td><td class="muted">${esc((t.reason||'').slice(0,60))}</td></tr>`).join('')
    : '<tr><td colspan="6" class="muted">история сделок пуста</td></tr>';

  // metrics — empty-state until at least 2 history buckets exist
  const noData=!eq.has_data;
  const set=(id,v,cls)=>{const e=document.getElementById(id);e.textContent=v;
    if(cls!==undefined)e.className='val '+cls;};
  set('ret', noData?'—':(st.ret>=0?'+':'')+FMT2(st.ret)+' USDT',
      noData?undefined:(st.ret>=0?'ok':'bad'));
  set('retpct', noData?'нужно ≥2 точки':(st.ret_pct>=0?'+':'')+st.ret_pct.toFixed(2)+'%',
      noData?undefined:(st.ret_pct>=0?'ok':'bad'));
  document.getElementById('retsub').textContent = noData
    ? 'снапшоты пишутся раз в 2 ч'
    : 'за '+st.span_days.toFixed(0)+' дн'
      +(st.apr_pct==null?'':' · APR '+(st.apr_pct>=0?'+':'')+st.apr_pct.toFixed(1)+'%');
  set('maxdd', noData?'—':'-'+FMT2(st.maxdd)+' ('+st.maxdd_pct.toFixed(2)+'%)',
      noData?undefined:'bad');
  document.getElementById('chartnote').textContent = noData
    ? '⚠️ История PnL ещё копится (снапшот каждые 2 ч; live-точка уже на графике).'
    : 'Снапшотов: '+eq.snapshots+' · точек: '+eq.buckets;

  // charts (eq/st already destructured at the top of render())
  mk('chEquity',{type:'line',data:{labels:eq.labels,datasets:[{
    label:'Equity, USDT',data:eq.equity,borderColor:'#58a6ff',
    backgroundColor:'rgba(88,166,255,.08)',fill:true,tension:.25,
    pointRadius:eq.equity.length>60?0:3,pointHoverRadius:5,borderWidth:2}]},
    options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false}},
      scales:{x:{ticks:{maxTicksLimit:12}},y:{grid:{color:GRID}}}}});

  mk('chPnl',{type:'bar',data:{labels:eq.pnl_labels,datasets:[{
    label:'PnL, USDT',data:eq.pnl_values,
    backgroundColor:eq.pnl_values.map(v=>v>=0?'#3fb95066':'#f8514966'),
    borderColor:eq.pnl_values.map(v=>v>=0?'#3fb950':'#f85149'),
    borderWidth:1}]},
    options:{responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false}},
      scales:{x:{ticks:{maxTicksLimit:12}},y:{grid:{color:GRID}}}}});

  mk('chFund',{type:'bar',data:{labels:d.scan.map(r=>r.symbol.replace('USDT','')),
    datasets:[{label:'% / 8ч',data:d.scan.map(r=>r.funding_pct),
      backgroundColor:d.scan.map(r=>r.funding>=0?'#3fb95099':'#f8514999'),
      borderRadius:3}]},
    options:{indexAxis:'y',responsive:true,maintainAspectRatio:false,
      plugins:{legend:{display:false},tooltip:{callbacks:{label:c=>
        c.parsed.x.toFixed(4)+'% / 8ч  ('+(c.parsed.x*3*365).toFixed(1)+'% год.)'}}},
      scales:{x:{grid:{color:GRID},ticks:{callback:v=>v.toFixed(2)+'%'}},
              y:{grid:{display:false}}}}});

  mk('chPos',{type:'doughnut',data:{labels:d.positions.map(p=>p.symbol),
    datasets:[{data:d.positions.map(p=>p.notional),
      backgroundColor:['#58a6ff','#3fb950','#d29922','#bc8cff','#f78166',
                       '#39c5cf','#ff7b72','#79c0ff'],
      borderColor:'#161b22',borderWidth:2}]},
    options:{responsive:true,maintainAspectRatio:false,cutout:'62%',
      plugins:{legend:{position:'right'},
        tooltip:{callbacks:{label:c=>c.label+': $'+FMT2(c.parsed)}}}}});
}

async function tick(){
  try{
    const b=new URLSearchParams(location.search).get('bucket')||'day';
    const r=await fetch('/api/status?bucket='+b);
    render(await r.json());
  }catch(e){console.error(e);}
  setTimeout(tick, REFRESH_MS);
}
window.addEventListener('DOMContentLoaded',tick);
"""

PAGE = """<!DOCTYPE html><html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<noscript><meta http-equiv="refresh" content="10"></noscript>
<title>Carry Bot Dashboard</title>
<link rel="icon" href="data:,">
<script src="/static/chart.umd.js"></script>
<style>__CSS__</style>
<script>const REFRESH_MS=__REFRESH_MS__;</script>
<script>__JS__</script>
</head><body>
<h1>📊 Carry Bot — Bybit mainnet</h1>
<p class="sub">только чтение · живое обновление · <span id="ts">…</span></p>

<div class="cards">
  <div class="card"><div class="lbl">Бот (systemd)</div>
    <div class="val" id="svc">…</div><div class="lbl" id="uptime"></div></div>
  <div class="card"><div class="lbl">Total Equity</div>
    <div class="val" id="eq">…</div><div class="lbl" id="avail"></div></div>
  <div class="card"><div class="lbl">Позиций открыто</div>
    <div class="val" id="npos">…</div><div class="lbl" id="npossub"></div></div>
  <div class="card"><div class="lbl">Heartbeat</div>
    <div class="val" id="hb">…</div>
    <div class="lbl" id="hbt"></div></div>
  <div class="card"><div class="lbl">PnL за период</div>
    <div class="val" id="ret">…</div><div class="lbl" id="retpct"></div>
    <div class="lbl" id="retsub"></div></div>
  <div class="card"><div class="lbl">Max Drawdown</div>
    <div class="val" id="maxdd">…</div></div>
</div>
<p class="muted" style="font-size:.75rem;margin:4px 0 0" id="coins"></p>

<h2>Открытые позиции (перпетуалы)</h2>
<table><thead><tr><th>Символ</th><th>Сторона</th><th>Размер</th><th>Нотионал</th>
<th>Вход</th><th>Марк</th><th>uPnL</th><th>Реал.ПнЛ</th>
<th>Фандинг/8ч</th><th>Базис,bps</th><th>Ликв.</th></tr></thead>
<tbody id="posbody"></tbody></table>

<h2>Графики
  <span class="toggle">группировка:
    <a href="?bucket=day" class="__BD__">день</a>
    <a href="?bucket=week" class="__BW__">неделя</a>
    <a href="?bucket=month" class="__BM__">месяц</a>
  </span>
</h2>
<div class="grid2">
  <div class="chart-box tall"><canvas id="chEquity"></canvas></div>
  <div class="chart-box tall"><canvas id="chPnl"></canvas></div>
  <div class="chart-box"><canvas id="chFund"></canvas></div>
  <div class="chart-box"><canvas id="chPos"></canvas></div>
</div>
<p class="muted" style="font-size:.7rem;margin:4px 0 0" id="chartnote"></p>

<h2>Сканер фандинга — 14 символов</h2>
<table><thead><tr><th>Символ</th><th>Фандинг/8ч</th><th>Годовых</th>
<th>Базис,bps</th></tr></thead><tbody id="scanbody"></tbody></table>
<p class="muted" style="font-size:.7rem">★ — текущий топ-5 для ротации ·
кэш 60с</p>

<h2>История сделок (последние)</h2>
<table><thead><tr><th>Время (UTC)</th><th>Действие</th><th>Символ</th>
<th>Сторона</th><th>Кол-во</th><th>Причина</th></tr></thead>
<tbody id="trbody"></tbody></table>

<footer>carry-dashboard · read-only · никакие ордера не отправляются</footer>
</body></html>"""


@app.route("/")
def index():
    bucket = request.args.get("bucket", "day")
    if bucket not in ("day", "week", "month"):
        bucket = "day"
    page = (PAGE.replace("__CSS__", CSS)
                .replace("__JS__", JS)
                .replace("__REFRESH_MS__", str(REFRESH_S * 1000))
                .replace("__BD__", "on" if bucket == "day" else "")
                .replace("__BW__", "on" if bucket == "week" else "")
                .replace("__BM__", "on" if bucket == "month" else ""))
    return page


@app.route("/api/status")
def api_status():
    bucket = request.args.get("bucket", "day")
    if bucket not in ("day", "week", "month"):
        bucket = "day"
    return jsonify(collect(bucket))


def main() -> None:
    global _ex, REFRESH_S
    ap = argparse.ArgumentParser(description="Read-only carry bot dashboard")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default 127.0.0.1 — только локально)")
    ap.add_argument("--port", type=int, default=8500)
    ap.add_argument("--refresh", type=int, default=5,
                    help="poll interval seconds (default 5)")
    args = ap.parse_args()
    REFRESH_S = args.refresh

    _ex = BybitExchange(testnet=False)
    print(f"📊 dashboard: http://{args.host}:{args.port}  (refresh {args.refresh}s)")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
