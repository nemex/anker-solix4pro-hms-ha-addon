#!/usr/bin/env python3
"""
Anker Solix 4 Pro Controller v1.2.0
===================================
Hybrid zero-feed-in controller for Anker Solix 4 Pro + Hoymiles HMS-2000 & HMS-1600.

Regelkonzept (Hybrid-Modus):
- Die Solarbank 4 Pro regelt sich autark über das Anker Smart Meter Gen 2.
- Dieses Add-on liest die Solarbank-Werte rein passiv (read-only) über Modbus TCP (Port 502) aus.
- Das Add-on steuert die Hoymiles-Wechselrichter (HMS-2000 & optional HMS-1600) über Home Assistant.
- Wenn der Akku voll ist (SOC >= soc_normal_max), wird der Hoymiles-Überschuss gedrosselt, falls Einspeisung droht.
- Die Drosselung wird dynamisch auf beide Inverter aufgeteilt, proportional zu ihrer aktuellen Solarerzeugung (calc_hms_limits).
- Watchdog: Bei Ausfall der Sensoren werden die Hoymiles-Limits komplett geöffnet.
"""

import json
import csv
import logging
import os
import signal
import time
import requests
from datetime import datetime
from pathlib import Path
from pyModbusTCP.client import ModbusClient

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("anker_solix_controller")

# ---------------------------------------------------------------------------
# Konfiguration & Pfade
# ---------------------------------------------------------------------------
OPTIONS_PATH = "/data/options.json"
STATE_PATH   = "/data/controller_state.json"
CSV_PATH     = "/data/controller_log.csv"
TICK_S       = 5

def load_options() -> dict:
    with open(OPTIONS_PATH) as f:
        return json.load(f)

def load_state() -> dict:
    try:
        if Path(STATE_PATH).exists():
            with open(STATE_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return {
        "active_mode": "smart_meter",
        "last_hms_limit": 3600.0,
        "last_hms_2000_lim": 2000.0,
        "last_hms_1600_lim": 1600.0,
        "grid_p_filtered": 0.0,
        "solar_p_last": 0.0,
        "haus_p_last": 0.0,
        "soc": 0.0,
        "pv_last": 0.0,
    }

def save_state(state: dict):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log.error("State speichern Fehler: %s", e)

# ---------------------------------------------------------------------------
# CSV Logging
# ---------------------------------------------------------------------------
CSV_FIELDS = [
    "ts", "mode", "soc", "grid_p", "haus_p", "solar_p",
    "battery_p", "pv", "load_p", "hms_limit",
    "hms_2000_power", "hms_2000_lim", "hms_2000_online",
    "hms_1600_power", "hms_1600_lim", "hms_1600_online"
]

CSV_MAX_BYTES = 2 * 1024 * 1024   # 2 MB
CSV_KEEP_LINES = 2000             # Datenzeilen, die beim Trimmen erhalten bleiben

def trim_csv(path: str, keep_lines: int = CSV_KEEP_LINES):
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= keep_lines + 1:
            return
        header = lines[0]
        tail = lines[-keep_lines:]
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write(header)
            f.writelines(tail)
        log.info("CSV gekürzt auf letzte %d Zeilen.", keep_lines)
    except Exception as e:
        log.debug("trim_csv Fehler: %s", e)

def csv_log(row: dict):
    try:
        write_header = not os.path.exists(CSV_PATH)
        if os.path.exists(CSV_PATH):
            try:
                with open(CSV_PATH, "r", encoding="utf-8") as f:
                    header = f.readline().strip().split(",")
                if header != CSV_FIELDS:
                    log.warning("CSV Header veraltet. Lösche alte CSV-Datei.")
                    os.remove(CSV_PATH)
                    write_header = True
            except Exception as e:
                log.error("Fehler beim Überprüfen des CSV-Headers: %s", e)
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if write_header:
                w.writeheader()
            w.writerow(row)
        if os.path.getsize(CSV_PATH) > CSV_MAX_BYTES:
            trim_csv(CSV_PATH)
    except Exception as e:
        log.debug("CSV log Fehler: %s", e)

# ---------------------------------------------------------------------------
# Home Assistant API
# ---------------------------------------------------------------------------
HA_URL   = "http://supervisor/core"
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
DRY_RUN  = True

HA_SESSION = requests.Session()
HA_SESSION.headers.update({"Authorization": f"Bearer {HA_TOKEN}"})
DEV_SESSION = requests.Session()

RUNNING = True

def _handle_term(signum, frame):
    global RUNNING
    log.info("Signal %s empfangen — beende nach aktuellem Tick...", signum)
    RUNNING = False

def sleep_tick(seconds: float):
    end = time.monotonic() + max(0.0, seconds)
    while RUNNING and time.monotonic() < end:
        time.sleep(0.2)

_SAVE_COUNTER = {"n": 0}

def save_state_throttled(state: dict, every: int = 6):
    _SAVE_COUNTER["n"] += 1
    if _SAVE_COUNTER["n"] >= every:
        _SAVE_COUNTER["n"] = 0
        save_state(state)

def ha_get_state(entity_id: str, default=None):
    if not entity_id or entity_id.lower() in ("none", ""):
        return default
    try:
        r = HA_SESSION.get(f"{HA_URL}/api/states/{entity_id}", timeout=5)
        if r.status_code == 200:
            state = r.json().get("state")
            if state not in ("unknown", "unavailable", None):
                return state
    except Exception as e:
        log.error("HA GET %s: %s", entity_id, e)
    return default

def ha_get_full(entity_id: str):
    if not entity_id or entity_id.lower() in ("none", ""):
        return None
    try:
        r = HA_SESSION.get(f"{HA_URL}/api/states/{entity_id}", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def ha_set_number(entity_id: str, value: float) -> bool:
    if not entity_id or entity_id.lower() in ("none", ""):
        return False
    if DRY_RUN:
        log.info("🔍 [DRY-RUN] WÜRDE setzen: %s = %s", entity_id, round(value, 1))
        return True
    try:
        r = HA_SESSION.post(
            f"{HA_URL}/api/services/number/set_value",
            json={"entity_id": entity_id, "value": round(value, 1)},
            timeout=5,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        log.error("HA SET %s: %s", entity_id, e)
        return False

def ha_push_sensor(entity_id: str, value: float, unit: str = "W", device_class: str = "power", friendly_name: str = "") -> bool:
    if DRY_RUN:
        return True
    try:
        payload = {
            "state": str(round(value, 1)),
            "attributes": {
                "unit_of_measurement": unit,
                "device_class": device_class,
                "state_class": "measurement",
                "friendly_name": friendly_name or entity_id,
            }
        }
        r = HA_SESSION.post(
            f"{HA_URL}/api/states/{entity_id}",
            json=payload,
            timeout=5,
        )
        return r.status_code in (200, 201)
    except Exception as e:
        log.debug("ha_push_sensor %s: %s", entity_id, e)
        return False

# ---------------------------------------------------------------------------
# Shelly Pro 3EM Direkt-Fallback
# ---------------------------------------------------------------------------
def shelly_direct_power(ip: str):
    if not ip or ip.lower() in ("none", ""):
        return None
    try:
        r = DEV_SESSION.get(f"http://{ip}/rpc/EM.GetStatus?id=0", timeout=3)
        if r.status_code == 200:
            val = r.json().get("total_act_power")
            if val is not None:
                return float(val)
    except Exception as e:
        log.debug("Shelly Direkt-Fallback Fehler: %s", e)
    return None

# ---------------------------------------------------------------------------
# Modbus TCP Hilfsfunktionen (Anker Solix 4 Pro)
# ---------------------------------------------------------------------------
def get_modbus_client(ip: str) -> ModbusClient:
    return ModbusClient(host=ip, port=502, auto_open=True, auto_close=True)

def read_input_uint16(client: ModbusClient, address: int) -> int | None:
    try:
        regs = client.read_input_registers(address, 1)
        if regs:
            return regs[0]
    except Exception as e:
        log.error("Modbus read input register %d (UINT16) error: %s", address, e)
    return None

def read_input_int32(client: ModbusClient, address: int) -> int | None:
    try:
        regs = client.read_input_registers(address, 2)
        if regs and len(regs) == 2:
            high = regs[0] & 0xFFFF
            low = regs[1] & 0xFFFF
            unsigned = (high << 16) | low
            if unsigned & 0x80000000:
                return -((~unsigned & 0xFFFFFFFF) + 1)
            else:
                return unsigned
    except Exception as e:
        log.error("Modbus read input register %d (INT32) error: %s", address, e)
    return None

# ---------------------------------------------------------------------------
# HMS Limits berechnen (Dynamisch nach Ist-Erzeugung)
# ---------------------------------------------------------------------------
def calc_hms_limits(
    hms_limit_target: float,
    solar_p_2000: float,
    solar_p_1600: float,
    hms_2000_online: bool,
    hms_1600_online: bool,
) -> tuple[float, float]:
    """Teilt das berechnete Gesamt-HMS-Limit stufenlos auf die beiden Inverter auf."""
    max_2000 = 2000.0
    max_1600 = 1600.0

    if not hms_1600_online:
        return min(hms_limit_target, max_2000), 0.0
    if not hms_2000_online:
        return 0.0, min(hms_limit_target, max_1600)

    # Wenn das Gesamtlimit auf Maximum steht, öffnen wir beide voll
    if hms_limit_target >= 3550.0:
        return max_2000, max_1600

    total_solar = solar_p_2000 + solar_p_1600

    if total_solar > 50.0:
        ratio_2000 = solar_p_2000 / total_solar
        ratio_1600 = solar_p_1600 / total_solar
    else:
        # Fallback auf Nennleistungsverhältnis bei Dunkelheit/Nacht
        ratio_2000 = 0.55
        ratio_1600 = 0.45

    limit_2000 = min(round(hms_limit_target * ratio_2000), max_2000)
    limit_2000 = max(0.0, limit_2000)

    limit_1600 = min(round(hms_limit_target * ratio_1600), max_1600)
    limit_1600 = max(0.0, limit_1600)

    return limit_2000, limit_1600

# ---------------------------------------------------------------------------
# Hauptregelschleife
# ---------------------------------------------------------------------------
def main():
    global DRY_RUN, RUNNING
    log.info("Anker Solix 4 Pro Controller v1.2.0 startet...")
    
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    
    opts  = load_options()
    state = load_state()

    DRY_RUN = bool(opts.get("dry_run", True))
    if DRY_RUN:
        log.info("DRY-RUN Modus aktiv - es wird nichts an Home Assistant gesendet!")
    else:
        log.info("Aktiver Modus - Steuerung drosselt Inverter in Home Assistant.")

    # Konfiguration laden
    grid_sensor               = opts["grid_sensor"]
    soc_sensor                = opts["soc_sensor"]
    solix_ip                  = opts["solix_ip"]
    shelly_ip                 = opts["shelly_ip"]
    soc_normal_max            = float(opts.get("soc_normal_max", 95))

    hms_2000_entity           = opts["hms_2000_entity"]
    hms_2000_power_sensor     = opts.get("hms_2000_power_sensor", "sensor.hoymiles_hms_2000_4t_power")
    hms_2000_reachable_sensor = opts.get("hms_2000_reachable_sensor", "binary_sensor.hoymiles_hms_2000_4t_reachable")
    
    hms_1600_entity           = opts.get("hms_1600_entity", "")
    hms_1600_power_sensor     = opts.get("hms_1600_power_sensor", "")
    hms_1600_reachable_sensor = opts.get("hms_1600_reachable_sensor", "")

    has_1600 = bool(hms_1600_entity and hms_1600_entity.lower() not in ("none", ""))

    client = get_modbus_client(solix_ip)

    # Initialisierung
    if "last_hms_limit" not in state:
        state["last_hms_limit"] = 3600.0 if has_1600 else 2000.0
    if "last_hms_2000_lim" not in state:
        state["last_hms_2000_lim"] = 2000.0
    if "last_hms_1600_lim" not in state:
        state["last_hms_1600_lim"] = 1600.0

    state["active_mode"] = "smart_meter"
    save_state(state)
    log.info("Controller initialisiert. Solix IP=%s, Shelly IP=%s, HMS-1600 aktiv=%s", solix_ip, shelly_ip, has_1600)

    # Lokaler Cache für Modbus-Daten bei Verbindungsproblemen
    solix_soc = float(state.get("soc", 50.0))
    solix_pv = 0.0
    solix_battery_p = 0.0
    solix_ac_output = 0.0
    solix_load_p = 0.0

    # Trägheitstakt für periodische Updates
    is_tick = 0

    while RUNNING:
        try:
            tick_start = time.monotonic()
            is_tick += 1

            # ------------------------------------------------------------------
            # 1. Messwerte lesen (Shelly / HA)
            # ------------------------------------------------------------------
            grid_val = ha_get_state(grid_sensor)
            if grid_val not in (None, "unknown", "unavailable"):
                grid_p_raw = float(grid_val)
            else:
                grid_p_raw = float(state.get("grid_p_filtered", 0.0))
                log.warning("Grid-Sensor (%s) offline — verwende letzten Wert %.0fW", grid_sensor, grid_p_raw)

            # HMS-2000 Leistung lesen
            hms_2000_power_val = ha_get_state(hms_2000_power_sensor)
            if hms_2000_power_val not in (None, "unknown", "unavailable"):
                solar_p_2000 = abs(float(hms_2000_power_val))
            else:
                solar_p_2000 = float(state.get("hms_2000_power_last", 0.0))

            hms_2000_online = (ha_get_state(hms_2000_reachable_sensor, "off") == "on") or (solar_p_2000 > 10.0)

            # HMS-1600 Leistung lesen
            solar_p_1600 = 0.0
            hms_1600_online = False
            if has_1600:
                hms_1600_power_val = ha_get_state(hms_1600_power_sensor)
                if hms_1600_power_val not in (None, "unknown", "unavailable"):
                    solar_p_1600 = abs(float(hms_1600_power_val))
                else:
                    solar_p_1600 = float(state.get("hms_1600_power_last", 0.0))

                hms_1600_online = (ha_get_state(hms_1600_reachable_sensor, "off") == "on") or (solar_p_1600 > 10.0)

            # Gesamt-Solarleistung der Inverter
            solar_p_inverters = (solar_p_2000 if hms_2000_online else 0.0) + (solar_p_1600 if hms_1600_online else 0.0)

            # Watchdog: Überprüfung, ob der Grid-Zähler frische Werte liefert
            shelly_data = ha_get_full(grid_sensor)
            if shelly_data:
                last_upd = shelly_data.get("last_updated", "")
                try:
                    upd_ts = datetime.fromisoformat(last_upd.replace("Z", "+00:00")).timestamp()
                    watchdog_ok = (time.time() - upd_ts) < 60
                except Exception:
                    watchdog_ok = False
            else:
                watchdog_ok = False

            # Fallback direkt über Shelly IP
            if not watchdog_ok and shelly_ip:
                direct_p = shelly_direct_power(shelly_ip)
                if direct_p is not None:
                    grid_p_raw = direct_p
                    watchdog_ok = True
                    log.warning("HA-API/Grid-Sensor stale — direkt vom Shelly gelesen: %.0fW", direct_p)

            # Sicherheits-Stopp bei Ausfall des Zählers (Hoymiles voll öffnen)
            if not watchdog_ok:
                log.warning("Watchdog Fehler! Zählerwerte eingefroren — öffne Limits zur Sicherheit.")
                if hms_2000_online:
                    ha_set_number(hms_2000_entity, 2000)
                    state["last_hms_2000_lim"] = 2000.0
                if has_1600 and hms_1600_online:
                    ha_set_number(hms_1600_entity, 1600)
                    state["last_hms_1600_lim"] = 1600.0
                state["last_hms_limit"] = 3600.0 if has_1600 else 2000.0
                save_state(state)
                sleep_tick(TICK_S)
                continue

            # ------------------------------------------------------------------
            # 2. Modbus-Daten vom Anker Solix 4 Pro lesen
            # ------------------------------------------------------------------
            soc_mb = read_input_uint16(client, 10014)
            if soc_mb is not None:
                solix_soc = float(soc_mb)
            else:
                # Fallback über Home Assistant Entity
                soc_ha = ha_get_state(soc_sensor)
                if soc_ha not in (None, "unknown", "unavailable"):
                    solix_soc = float(soc_ha)
                else:
                    log.warning("BMS Modbus & HA offline — verwende letzten SOC %.1f%%", solix_soc)

            state["soc"] = solix_soc

            pv_mb = read_input_int32(client, 10002)
            if pv_mb is not None:
                solix_pv = float(pv_mb)
                # Dritter Kanal (falls vorhanden, Register 10004)
                pv_3rd = read_input_int32(client, 10004)
                if pv_3rd is not None:
                    solix_pv += float(pv_3rd)
            
            bat_p_mb = read_input_int32(client, 10008)
            if bat_p_mb is not None:
                solix_battery_p = float(bat_p_mb)

            ac_out_mb = read_input_int32(client, 10208)
            if ac_out_mb is not None:
                solix_ac_output = float(ac_out_mb)

            load_p_mb = read_input_int32(client, 10010)
            if load_p_mb is not None:
                solix_load_p = float(load_p_mb)

            # ------------------------------------------------------------------
            # 3. Hausverbrauch berechnen
            # ------------------------------------------------------------------
            haus_p = max(0.0, grid_p_raw + solar_p_inverters + solix_ac_output)

            # ------------------------------------------------------------------
            # 4. Sonnenstand & Moduswahl
            # ------------------------------------------------------------------
            # Prüfen ob Sonne oben ist (über HA Sun-Modul oder einfach tagsüber)
            sun_above = (ha_get_state("sun.sun", "below_horizon") == "above_horizon") or (solix_pv > 10.0) or (solar_p_inverters > 10.0)

            if not sun_above:
                state["active_mode"] = "night"
            elif solix_soc >= soc_normal_max:
                state["active_mode"] = "drosselung"
            else:
                state["active_mode"] = "smart_meter"

            # ------------------------------------------------------------------
            # 5. Drosselungsregelung
            # ------------------------------------------------------------------
            # Der Netzbezug-Sollwert ist 10W, um eine minimale Einspeisung zu verhindern.
            grid_target = 10.0
            grid_error = grid_p_raw - grid_target

            # Maximal mögliches Limit der aktiven Inverter ermitteln
            max_limit = (2000.0 if hms_2000_online else 0.0) + (1600.0 if hms_1600_online else 0.0)
            if max_limit == 0.0:
                max_limit = 3600.0 if has_1600 else 2000.0

            hms_limit_last = float(state.get("last_hms_limit", max_limit))
            hms_limit_new = hms_limit_last

            # Drossel-Auslöser: Wenn der Akku voll ist und Einspeisung vorliegt
            if state["active_mode"] == "drosselung":
                # Empfindlicher Schwellwert von -20W im Smart-Meter-Modus
                if grid_error < -20.0:
                    # Einspeisung: Hoymiles drosseln
                    hms_limit_new = hms_limit_last + grid_error * 0.5
                elif grid_error > 20.0:
                    # Netzbezug: Hoymiles freigeben
                    hms_limit_new = hms_limit_last + grid_error * 0.5
            else:
                # Akku nicht voll: Beide Inverter voll öffnen, damit Anker Smart Meter den Überschuss einlagern kann.
                hms_limit_new = max_limit

            hms_limit_new = max(0.0, min(max_limit, hms_limit_new))
            hms_limit_new_rounded = round(hms_limit_new / 10.0) * 10.0

            # Dynamic actual-production ratio splitting
            limit_2000, limit_1600 = calc_hms_limits(
                hms_limit_new_rounded, solar_p_2000, solar_p_1600,
                hms_2000_online, hms_1600_online
            )

            # Drossel-Status für Log / UI ermitteln
            drosseln = (hms_limit_new_rounded < max_limit - 100.0) and (solar_p_inverters >= hms_limit_new_rounded - 150.0)

            # ------------------------------------------------------------------
            # 6. Inverter limits über Home Assistant setzen
            # ------------------------------------------------------------------
            do_is_update = (is_tick % 6 == 0) # Alle 30 Sekunden forcieren

            if hms_2000_online:
                last_written_2000 = float(state.get("last_hms_2000_lim", 2000.0))
                need_send_2000 = (abs(last_written_2000 - limit_2000) >= 50) or (drosseln and solar_p_2000 > limit_2000 + 50 and do_is_update)
                if need_send_2000:
                    if ha_set_number(hms_2000_entity, limit_2000):
                        state["last_hms_2000_lim"] = limit_2000

            if has_1600 and hms_1600_online:
                last_written_1600 = float(state.get("last_hms_1600_lim", 1600.0))
                need_send_1600 = (abs(last_written_1600 - limit_1600) >= 50) or (drosseln and solar_p_1600 > limit_1600 + 50 and do_is_update)
                if need_send_1600:
                    if ha_set_number(hms_1600_entity, limit_1600):
                        state["last_hms_1600_lim"] = limit_1600

            log.info("Modus=%s SOC=%.1f%% | HMS-Limit=%dW (HMS-2000: %dW, HMS-1600: %dW) | grid=%.0fW haus=%.0fW solar=%.0fW",
                     state["active_mode"], solix_soc, int(hms_limit_new_rounded), int(limit_2000), int(limit_1600),
                     grid_p_raw, haus_p, solar_p_inverters + solix_pv)

            # ------------------------------------------------------------------
            # 7. Daten speichern & HA Sensoren aktualisieren
            # ------------------------------------------------------------------
            csv_log({
                "ts":             time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode":           state["active_mode"],
                "soc":            round(solix_soc, 1),
                "grid_p":         round(grid_p_raw, 1),
                "haus_p":         round(haus_p, 1),
                "solar_p":        round(solar_p_inverters + solix_pv, 1),
                "battery_p":      round(solix_battery_p, 1),
                "pv":             round(solix_pv, 1),
                "load_p":         round(solix_load_p, 1),
                "hms_limit":      round(hms_limit_new_rounded, 0),
                "hms_2000_power": round(solar_p_2000, 1),
                "hms_2000_lim":   round(limit_2000, 0),
                "hms_2000_online": 1 if hms_2000_online else 0,
                "hms_1600_power": round(solar_p_1600, 1),
                "hms_1600_lim":   round(limit_1600, 0),
                "hms_1600_online": 1 if hms_1600_online else 0,
            })

            # Virtuelle Sensoren an HA pushen
            ha_push_sensor("sensor.anker_hausverbrauch", haus_p, "W", "power", "Hausverbrauch (Anker Controller)")
            ha_push_sensor("sensor.anker_grid_p", grid_p_raw, "W", "power", "Netz aktuell (Anker Controller)")
            ha_push_sensor("sensor.anker_solar_p", solix_pv + solar_p_inverters, "W", "power", "Solar gesamt (Anker Controller)")
            ha_push_sensor("sensor.anker_battery_ac", solix_ac_output, "W", "power", "Batterie AC (Anker Controller)")
            ha_push_sensor("sensor.anker_solix_soc", solix_soc, "%", "battery", "Anker Solix SOC")
            ha_push_sensor("sensor.anker_solix_pv", solix_pv, "W", "power", "Anker Solix PV Leistung")
            ha_push_sensor("sensor.anker_solix_battery_power", solix_battery_p, "W", "power", "Anker Solix Batterie Leistung")
            ha_push_sensor("sensor.anker_solix_load_power", solix_load_p, "W", "power", "Anker Solix Last Leistung")

            state["grid_p_filtered"] = grid_p_raw
            state["solar_p_last"]    = solar_p_inverters
            state["hms_2000_power_last"] = solar_p_2000
            state["hms_1600_power_last"] = solar_p_1600
            state["haus_p_last"]     = haus_p
            state["last_hms_limit"]  = hms_limit_new_rounded
            state["pv_last"]         = solix_pv
            save_state_throttled(state)

        except Exception as e:
            log.error("Fehler im Regelzyklus: %s", e, exc_info=True)

        elapsed = time.monotonic() - tick_start
        sleep_tick(TICK_S - elapsed)

    # Shutdown-Safe-State
    log.info("Shutdown — öffne Hoymiles Limits voll zur Sicherheit...")
    try:
        ha_set_number(hms_2000_entity, 2000)
        if has_1600:
            ha_set_number(hms_1600_entity, 1600)
    except Exception as e:
        log.error("Fehler beim Shutdown-Safe-State: %s", e)
    log.info("Controller beendet.")

if __name__ == "__main__":
    main()
