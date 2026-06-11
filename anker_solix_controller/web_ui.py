#!/usr/bin/env python3
"""
Anker Solix 4 Pro Controller — Web UI v1.2.0
Port 8766:
  GET /        → Live Dashboard mit Chart
  GET /state   → JSON State
  GET /log     → CSV Log Download
  GET /api     → JSON für Chart (letzte 100 Datenpunkte)
  POST /log/delete → Log löschen
"""

import csv
import json
import os
import time
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

STATE_PATH = "/data/controller_state.json"
OPTIONS_PATH = "/data/options.json"
CSV_PATH = "/data/controller_log.csv"

def load_state() -> dict:
    try:
        if Path(STATE_PATH).exists():
            with open(STATE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def load_options() -> dict:
    try:
        if Path(OPTIONS_PATH).exists():
            with open(OPTIONS_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def get_csv_data(n=100) -> list:
    """Liest die letzten n Zeilen aus der CSV."""
    try:
        if not os.path.exists(CSV_PATH):
            return []
        with open(CSV_PATH, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        return rows[-n:]
    except Exception:
        return []

HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Anker Solix 4 Pro Controller</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {
    --bg: #0a0e14; --surface: #111822; --border: #1e2d3d;
    --accent: #f0a500; --green: #00e676; --red: #ff3d57;
    --blue: #00c2ff; --text: #cdd9e5; --muted: #4a5568;
    --mono: 'Courier New', monospace;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: sans-serif; padding: 20px; }
  h1 { color: var(--accent); font-size: 16px; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 20px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 20px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 14px; }
  .card-title { font-size: 10px; color: var(--muted); letter-spacing: 2px; text-transform: uppercase; margin-bottom: 6px; }
  .card-value { font-family: var(--mono); font-size: 24px; color: var(--blue); }
  .card-value.pos { color: var(--green); }
  .card-value.neg { color: var(--red); }
  .card-value.warn { color: var(--accent); }
  .mode { display: inline-block; padding: 3px 10px; border-radius: 3px; font-size: 11px; font-family: var(--mono); }
  .mode.smart_meter { background: rgba(0,194,255,0.15); color: var(--blue); border: 1px solid var(--blue); }
  .mode.drosselung { background: rgba(240,165,0,0.15); color: var(--accent); border: 1px solid var(--accent); }
  .mode.night { background: rgba(74,85,104,0.3); color: var(--muted); border: 1px solid var(--muted); }
  .soc-bar { height: 5px; background: var(--border); border-radius: 3px; margin-top: 6px; overflow: hidden; }
  .soc-fill { height: 100%; background: var(--green); border-radius: 3px; transition: width 1s; }
  .chart-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: 16px; margin-bottom: 20px; }
  .chart-wrap h2 { font-size: 11px; color: var(--muted); letter-spacing: 2px; text-transform: uppercase; margin-bottom: 12px; }
  canvas { max-height: 220px; }
  .footer { color: var(--muted); font-size: 11px; margin-top: 16px; display: flex; justify-content: space-between; }
  .btn { background: var(--surface); border: 1px solid var(--border); color: var(--accent); 
         font-family: var(--mono); font-size: 11px; padding: 6px 12px; border-radius: 3px; 
         cursor: pointer; text-decoration: none; }
  .btn:hover { border-color: var(--accent); }
</style>
</head>
<body>
<h1>⚡ Anker Solix 4 Pro Controller (Hybrid)</h1>
 
<div class="grid" id="cards">
  <div class="card"><div class="card-title">Modus</div><div id="c-mode" class="mode smart_meter">—</div></div>
  <div class="card"><div class="card-title">Netz</div><div id="c-grid" class="card-value">— W</div></div>
  <div class="card"><div class="card-title">Haus</div><div id="c-haus" class="card-value">— W</div></div>
  <div class="card"><div class="card-title">Solar (Gesamt)</div><div id="c-solar" class="card-value">— W</div></div>
  <div class="card"><div class="card-title">Anker PV Leistung</div><div id="c-anker-pv" class="card-value">— W</div></div>
  <div class="card"><div class="card-title">Anker Batterie</div><div id="c-battery-p" class="card-value">— W</div></div>
  <div class="card"><div class="card-title">Hoymiles Limit</div><div id="c-hms-lim" class="card-value">— W</div></div>
  <div class="card"><div class="card-title">Hoymiles Ist</div><div id="c-hms-pow" class="card-value">— W</div></div>
  <div class="card">
    <div class="card-title">SOC</div>
    <div id="c-soc" class="card-value">— %</div>
    <div class="soc-bar"><div id="c-soc-bar" class="soc-fill" style="width:0%"></div></div>
  </div>
</div>

<div class="chart-wrap">
  <h2>Netzleistung (letzte 100 Messwerte)</h2>
  <canvas id="chart-grid"></canvas>
</div>

<div class="chart-wrap">
  <h2>Solar / Haus / Hoymiles-Limit</h2>
  <canvas id="chart-power"></canvas>
</div>

<div class="chart-wrap">
  <h2>Wechselrichter — Ist vs. Limit</h2>
  <div style="margin-bottom:10px">
    <span style="font-size:10px;color:#4a5568">Status: </span>
    <span id="inv-status" style="font-size:11px;font-family:monospace">—</span>
  </div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:8px">
    <div>
      <div style="font-size:10px;color:#4a5568;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px">Anker Solarbank 4 Pro</div>
      <div style="display:flex;justify-content:space-between;margin-bottom:2px">
        <span style="font-size:11px;color:#cdd9e5">PV-Leistung: <span id="inv-se-ist">—</span> W</span>
        <span style="font-size:11px;color:#4a5568">Akkustrom: <span id="inv-se-bat">—</span> W</span>
      </div>
      <div style="height:8px;background:#1e2d3d;border-radius:4px;overflow:hidden;margin-bottom:4px">
        <div id="inv-se-bar" style="height:100%;background:#00e676;border-radius:4px;width:0%;transition:width 1s"></div>
      </div>
      <div id="inv-se-reason" style="font-size:10px;color:#4a5568">—</div>
    </div>
    <div>
      <div style="font-size:10px;color:#4a5568;letter-spacing:2px;text-transform:uppercase;margin-bottom:6px">Hoymiles Inverter (Gesamt)</div>
      <div style="display:flex;justify-content:space-between;margin-bottom:2px">
        <span style="font-size:11px;color:#cdd9e5">Ist: <span id="inv-2000-ist">—</span> W</span>
        <span style="font-size:11px;color:#4a5568">Limit: <span id="inv-2000-lim">—</span> W</span>
      </div>
      <div style="height:8px;background:#1e2d3d;border-radius:4px;overflow:hidden;margin-bottom:4px">
        <div id="inv-2000-bar" style="height:100%;background:#f0a500;border-radius:4px;width:0%;transition:width 1s"></div>
      </div>
      <div id="inv-2000-reason" style="font-size:10px;color:#4a5568">—</div>
    </div>
  </div>
</div>

<div class="footer">
  <span id="ts">—</span>
  <a href="log" class="btn">⬇ CSV Download</a>
  <button class="btn" style="margin-left:8px;border-color:#ff3d57;color:#ff3d57" onclick="deleteLog()">🗑 Log löschen</button>
</div>

<script>
const chartColors = {
  grid: '#ff3d57', haus: '#00c2ff', solar: '#f0a500',
  hms: '#7c3aed'
};

function makeChart(id, datasets, yLabel) {
  return new Chart(document.getElementById(id), {
    type: 'line',
    data: { labels: [], datasets },
    options: {
      responsive: true,
      animation: false,
      plugins: { legend: { labels: { color: '#cdd9e5', font: { size: 11 } } } },
      scales: {
        x: { ticks: { color: '#4a5568', maxTicksLimit: 8, font: { size: 10 } }, grid: { color: '#1e2d3d' } },
        y: { ticks: { color: '#4a5568', font: { size: 10 } }, grid: { color: '#1e2d3d' }, title: { display: true, text: yLabel, color: '#4a5568', font: { size: 10 } } }
      }
    }
  });
}

const gridChart = makeChart('chart-grid', [
  { label: 'Netz (W)', data: [], borderColor: chartColors.grid, backgroundColor: 'rgba(255,61,87,0.1)', tension: 0.3, pointRadius: 0, fill: true }
], 'Watt');

const powerChart = makeChart('chart-power', [
  { label: 'Solar gesamt (W)', data: [], borderColor: chartColors.solar, tension: 0.3, pointRadius: 0 },
  { label: 'Haus (W)', data: [], borderColor: chartColors.haus, tension: 0.3, pointRadius: 0 },
  { label: 'Hoymiles Limit (W)', data: [], borderColor: chartColors.hms, tension: 0.3, pointRadius: 0, borderDash: [4,2] }
], 'Watt');

function updateCards(state, csv) {
  let mode = state.active_mode || 'smart_meter';
  const el = document.getElementById('c-mode');
  el.textContent = { smart_meter: 'SMART METER REGELUNG', drosselung: 'AKKU VOLL / DROSSELUNG', night: 'NACHT' }[mode] || mode.toUpperCase();
  el.className = 'mode ' + mode;

  const grid = parseFloat(csv.grid_p || state.grid_p_filtered || 0);
  setCard('c-grid', grid.toFixed(0) + ' W', grid > 50 ? 'neg' : grid < -20 ? 'pos' : '');
  setCard('c-haus', parseFloat(csv.haus_p || state.haus_p_last || 0).toFixed(0) + ' W', '');
  setCard('c-solar', parseFloat(csv.solar_p || state.solar_p_last || 0).toFixed(0) + ' W', 'pos');
  setCard('c-anker-pv', parseFloat(csv.pv || state.pv_last || 0).toFixed(0) + ' W', 'pos');
  
  const battery_p = parseFloat(csv.battery_p || 0);
  setCard('c-battery-p', battery_p.toFixed(0) + ' W', battery_p < 0 ? 'pos' : battery_p > 0 ? 'warn' : '');
  
  const hmsLim = parseFloat(csv.hms_limit !== undefined ? csv.hms_limit : (state.last_hms_limit || 2000));
  setCard('c-hms-lim', hmsLim.toFixed(0) + ' W', '');
  
  const hms2000Power = parseFloat(csv.hms_2000_power || csv.hms_power || 0);
  const hms1600Power = parseFloat(csv.hms_1600_power || 0);
  const hmsPow = hms2000Power + hms1600Power;
  setCard('c-hms-pow', hmsPow.toFixed(0) + ' W', '');

  const soc = parseFloat(csv.soc || state.soc || 0);
  setCard('c-soc', soc.toFixed(0) + ' %', soc < 20 ? 'warn' : '');
  document.getElementById('c-soc-bar').style.width = Math.min(100, soc) + '%';
  document.getElementById('ts').textContent = 'Letzte Aktualisierung: ' + new Date().toLocaleTimeString('de-DE');

  // Status Ist vs Limit
  const seIst = parseFloat(csv.pv || state.pv_last || 0);
  const maxHms = state.last_hms_limit > 2000 ? 3600 : 2000;

  let statusText = '—';
  let seReason = '—', hmsReason = '—';

  if (mode === 'night') {
    statusText = '⚪ Nacht — kein aktiver Solarbetrieb';
    seReason = 'Nacht';
    hmsReason = 'Nacht';
  } else if (mode === 'drosselung') {
    statusText = '⬇ Akku voll — Drosselung aktiv (' + Math.round(grid) + 'W)';
    seReason = 'Akku voll (regelt autark)';
    hmsReason = hmsLim < maxHms ? '⬇ Gedrosselt (Überschuss)' : '✅ Offen';
  } else {
    statusText = '🟢 Smart Meter Regelung aktiv — Akku lädt (' + Math.round(grid) + 'W)';
    seReason = battery_p < 0 ? '🔋 Lädt mit ' + Math.abs(battery_p).toFixed(0) + 'W' : '✅ Autarke Regelung';
    hmsReason = '✅ Offen';
  }

  document.getElementById('inv-status').textContent = statusText;
  document.getElementById('inv-se-reason').textContent = seReason;
  document.getElementById('inv-2000-reason').textContent = hmsReason;

  document.getElementById('inv-se-ist').textContent = Math.round(seIst);
  document.getElementById('inv-se-bat').textContent = Math.round(battery_p);
  const seBarPct = Math.min(100, seIst / 2500 * 100);
  document.getElementById('inv-se-bar').style.width = seBarPct + '%';

  setInv('inv-2000', hmsPow, hmsLim, maxHms);
}

function setInv(prefix, ist, lim, max) {
  document.getElementById(prefix + '-ist').textContent = Math.round(ist);
  document.getElementById(prefix + '-lim').textContent = Math.round(lim);
  const pct = lim > 0 ? Math.min(100, ist / lim * 100) : (ist > 0 ? 100 : 0);
  const color = pct > 95 ? '#ff3d57' : pct > 70 ? '#f0a500' : '#00e676';
  const bar = document.getElementById(prefix + '-bar');
  bar.style.width = Math.min(100, ist / max * 100) + '%';
  bar.style.background = color;
}

function setCard(id, text, cls) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'card-value' + (cls ? ' ' + cls : '');
}

function updateCharts(rows) {
  const labels = rows.map(r => r.ts ? r.ts.split(' ')[1].substring(0,5) : '');
  
  gridChart.data.labels = labels;
  gridChart.data.datasets[0].data = rows.map(r => parseFloat(r.grid_p || 0));
  gridChart.update();

  powerChart.data.labels = labels;
  powerChart.data.datasets[0].data = rows.map(r => parseFloat(r.solar_p || 0));
  powerChart.data.datasets[1].data = rows.map(r => parseFloat(r.haus_p || 0));
  powerChart.data.datasets[2].data = rows.map(r => parseFloat(r.hms_limit || 0));
  powerChart.update();
}

async function deleteLog() {
  if (!confirm('CSV-Log wirklich löschen?')) return;
  try {
    const r = await fetch('log/delete', { method: 'POST' });
    const d = await r.json();
    if (d.status === 'ok') alert('Log gelöscht!');
    else alert('Fehler: ' + d.message);
  } catch(e) { alert('Fehler!'); }
}

async function refresh() {
  try {
    const stateR = await fetch('state').then(r => r.json());
    updateCards(stateR, {});
    
    try {
      const apiR = await fetch('api').then(r => r.json());
      if (apiR.rows && apiR.rows.length > 0) {
        updateCards(stateR, apiR.rows[apiR.rows.length - 1]);
        updateCharts(apiR.rows);
      }
    } catch(e) {
      console.log('API Fehler:', e);
    }
  } catch(e) {
    console.log('State Fehler:', e);
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""

class UIHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        opts = load_options()

        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())

        elif self.path == "/state":
            st = load_state()
            try:
                st["soc_normal_max"] = float(opts.get("soc_normal_max", 95))
            except Exception:
                st["soc_normal_max"] = 95
            self._json(st)

        elif self.path == "/api":
            rows = get_csv_data(100)
            self._json({"rows": rows})

        elif self.path == "/log":
            if os.path.exists(CSV_PATH):
                with open(CSV_PATH, "r", encoding="utf-8") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/csv")
                self.send_header("Content-Disposition", "attachment; filename=controller_log.csv")
                self.end_headers()
                self.wfile.write(data.encode())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Noch keine Logdaten")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/log/delete":
            try:
                if os.path.exists(CSV_PATH):
                    os.remove(CSV_PATH)
                self._json({"status": "ok", "message": "Log gelöscht"})
            except Exception as e:
                self._json({"status": "error", "message": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def _json(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8766), UIHandler)
    print("Web UI läuft auf http://0.0.0.0:8766")
    server.serve_forever()
