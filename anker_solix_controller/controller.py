#!/usr/bin/env python3
"""
Anker Solix 4 Pro Controller v1.0.0
===================================
Zero-feed-in controller for Anker Solix 4 Pro with Hoymiles HMS inverters.

Regelkonzept:
- Modbus TCP: Steuerung der Solarbank 4 Pro über Register 10071 (battery_power_setpoint)
- Home Assistant: Regelung des Hoymiles-Wechselrichters über Limit-Entities
- Nachts: Setpoint-Regelung basierend auf dem Hausverbrauch (Entladung)
- Zwangsladung: Alle calibration_days Tage auf 100% zur BMS-Kalibrierung
- Watchdog: Übergabe an Geräteregelung bei Fehlern (Self-consumption / Mode 0)
"""

import json
import csv
import logging
import os
import signal
import time
import requests
from datetime import datetime, timezone, timedelta
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
# Konfiguration
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
        "active_mode": "night",
        "last_calibration_ts": time.time(),
        "grid_p_filtered": 0.0,
        "solar_p_last": 0.0,
        "haus_p_last": 0.0,
        "last_setpoint": 0.0,
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
    "setpoint", "battery_p", "pv", "load_p", "hms_limit",
    "hms_power", "hms_online"
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
    try:
        r = HA_SESSION.get(f"{HA_URL}/api/states/{entity_id}", timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None

def ha_set_number(entity_id: str, value: float) -> bool:
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
# Sonnenstand
# ---------------------------------------------------------------------------
def get_sun_state() -> dict:
    data = ha_get_full("sun.sun")
    if data:
        return data
    return {"state": "below_horizon"}

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

def read_holding_uint16(client: ModbusClient, address: int) -> int | None:
    try:
        regs = client.read_holding_registers(address, 1)
        if regs:
            return regs[0]
    except Exception as e:
        log.error("Modbus read holding register %d (UINT16) error: %s", address, e)
    return None

def read_holding_int32(client: ModbusClient, address: int) -> int | None:
    try:
        regs = client.read_holding_registers(address, 2)
        if regs and len(regs) == 2:
            high = regs[0] & 0xFFFF
            low = regs[1] & 0xFFFF
            unsigned = (high << 16) | low
            if unsigned & 0x80000000:
                return -((~unsigned & 0xFFFFFFFF) + 1)
            else:
                return unsigned
    except Exception as e:
        log.error("Modbus read holding register %d (INT32) error: %s", address, e)
    return None

def write_holding_uint16(client: ModbusClient, address: int, value: int) -> bool:
    if DRY_RUN:
        log.info("🔍 [DRY-RUN] Modbus Write holding register %d (UINT16) = %d", address, value)
        return True
    try:
        res = client.write_single_register(address, int(value) & 0xFFFF)
        return bool(res)
    except Exception as e:
        log.error("Modbus write holding register %d (UINT16) error: %s", address, e)
    return False

def write_holding_int32(client: ModbusClient, address: int, value: int) -> bool:
    if DRY_RUN:
        log.info("🔍 [DRY-RUN] Modbus Write holding register %d (INT32) = %d", address, value)
        return True
    try:
        int_value = int(value)
        if int_value < 0:
            int_value += 0x100000000
        high = (int_value >> 16) & 0xFFFF
        low = int_value & 0xFFFF
        res = client.write_multiple_registers(address, [high, low])
        return bool(res)
    except Exception as e:
        log.error("Modbus write holding register %d (INT32) error: %s", address, e)
    return False

# ---------------------------------------------------------------------------
# Hauptregelschleife
# ---------------------------------------------------------------------------
def main():
    global DRY_RUN, RUNNING
    log.info("Anker Solix 4 Pro Controller v1.0.0 startet...")
    
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)
    
    opts  = load_options()
    state = load_state()

    DRY_RUN = bool(opts.get("dry_run", True))
    if DRY_RUN:
        log.info("DRY-RUN Modus aktiv - es wird nichts geschrieben!")
    else:
        log.info("Aktiver Modus - Steuerung schreibt in HA und Modbus")

    # Konfiguration laden
    grid_sensor               = opts["grid_sensor"]
    soc_sensor                = opts["soc_sensor"]
    hms_2000_entity           = opts["hms_2000_entity"]
    hms_2000_power_sensor     = opts.get("hms_2000_power_sensor", "sensor.hoymiles_hms_2000_4t_power")
    hms_2000_reachable_sensor = opts.get("hms_2000_reachable_sensor", "binary_sensor.hoymiles_hms_2000_4t_reachable")
    
    soc_normal_max            = float(opts.get("soc_normal_max", 95))
    soc_min                   = float(opts.get("soc_min", 10))
    calib_days                = float(opts.get("calibration_days", 15))
    solix_ip                  = opts["solix_ip"]
    shelly_ip                 = opts["shelly_ip"]
    anker_smart_meter_active  = bool(opts.get("anker_smart_meter_active", False))

    client = get_modbus_client(solix_ip)

    # Initialisierung
    if "last_calibration_ts" not in state:
        state["last_calibration_ts"] = time.time()
    if "last_hms_limit" not in state:
        state["last_hms_limit"] = 2000.0
    if "manual_feed_in_active" not in state:
        state["manual_feed_in_active"] = False
    if "manual_feed_in_accumulated_kwh" not in state:
        state["manual_feed_in_accumulated_kwh"] = 0.0

    state["active_mode"] = "night"
    save_state(state)
    log.info("Controller initialisiert, Solix IP=%s, Shelly IP=%s", solix_ip, shelly_ip)

    # Lokaler Cache für Modbus-Daten bei Ausfällen
    solix_soc = float(state.get("soc", 50.0))
    solix_pv = 0.0
    solix_battery_p = 0.0
    solix_ac_output = 0.0
    solix_load_p = 0.0
    solix_max_charge = 2000.0
    solix_max_discharge = 2500.0

    while RUNNING:
        try:
            tick_start = time.monotonic()

            # ------------------------------------------------------------------
            # 1. Messwerte lesen (Shelly / HA)
            # ------------------------------------------------------------------
            grid_val = ha_get_state(grid_sensor)
            if grid_val not in (None, "unknown", "unavailable"):
                grid_p_raw = float(grid_val)
            else:
                grid_p_raw = float(state.get("grid_p_filtered", 0.0))
                log.warning("Grid-Sensor (%s) offline — verwende letzten Wert %.0fW", grid_sensor, grid_p_raw)

            # Hoymiles HMS-2000 Leistung lesen
            hms_power_val = ha_get_state(hms_2000_power_sensor)
            if hms_power_val not in (None, "unknown", "unavailable"):
                solar_p_hms = abs(float(hms_power_val))
            else:
                solar_p_hms = float(state.get("solar_p_last", 0.0))
                log.debug("HMS-2000 Power-Sensor offline — verwende letzten Wert %.0fW", solar_p_hms)

            hms_online = (ha_get_state(hms_2000_reachable_sensor, "off") == "on") or (solar_p_hms > 10.0)

            # Watchdog: Überprüfung, ob der Zähler frische Werte liefert
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

            # Sicherheits-Fallback direkt über Shelly IP
            if not watchdog_ok:
                direct_p = shelly_direct_power(shelly_ip)
                if direct_p is not None:
                    grid_p_raw = direct_p
                    watchdog_ok = True
                    log.warning("HA-API/Grid-Sensor stale — direkt vom Shelly gelesen: %.0fW", direct_p)

            # Sicherheits-Stopp bei Watchdog-Fehler
            if not watchdog_ok:
                log.warning("Watchdog Fehler! Grid-Sensor und Shelly-Direktzugriff offline — Sicherheits-Stopp.")
                # Solarbank auf Self-Consumption-Modus (0) setzen, damit sie sich selbst regelt
                if not anker_smart_meter_active:
                    write_holding_uint16(client, 10064, 0)
                # Hoymiles voll öffnen
                ha_set_number(hms_2000_entity, 2000)
                state["last_hms_limit"] = 2000.0
                save_state(state)
                sleep_tick(TICK_S)
                continue

            # ------------------------------------------------------------------
            # 2. Modbus-Daten vom Anker Solix 4 Pro lesen
            # ------------------------------------------------------------------
            # Verbindung sicherstellen
            soc_mb = read_input_uint16(client, 10014)
            if soc_mb is not None:
                solix_soc = float(soc_mb)
            else:
                # Falls Modbus fehlschlägt, versuchen wir das HA-Entity
                soc_ha = ha_get_state(soc_sensor)
                if soc_ha not in (None, "unknown", "unavailable"):
                    solix_soc = float(soc_ha)
                else:
                    log.warning("BMS Modbus & HA offline — verwende letzten SOC %.1f%%", solix_soc)

            state["soc"] = solix_soc

            pv_mb = read_input_int32(client, 10002)
            if pv_mb is not None:
                # Gesamt-PV = PCS PV + 3rd Party PV (falls vorhanden, lesen wir 10002 und addieren)
                solix_pv = float(pv_mb)
                # Falls dritter PV-Kanal vorhanden (Adresse 10004)
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

            # Max limits auslesen
            max_c = read_input_int32(client, 10036)
            if max_c is not None:
                solix_max_charge = float(max_c)
            max_d = read_input_int32(client, 10038)
            if max_d is not None:
                solix_max_discharge = float(max_d)

            # Hysterese für SOC Entladeschutz
            low_soc_active = state.get("low_soc_active", False)
            if solix_soc <= soc_min:
                low_soc_active = True
            elif solix_soc >= (soc_min + 2.0):
                low_soc_active = False
            state["low_soc_active"] = low_soc_active

            # ------------------------------------------------------------------
            # 3. Hausverbrauch & manuelle Einspeisung
            # ------------------------------------------------------------------
            # Hausverbrauch berechnen: Netzleistung + Hoymiles + Solarbank-AC-Ausgang
            # Hinweis: solix_ac_output ist positiv beim Einspeisen und negativ beim Laden.
            haus_p = max(0.0, grid_p_raw + (solar_p_hms if hms_online else 0.0) + solix_ac_output)

            manual_feed_in_switch  = opts.get("manual_feed_in_switch", "input_boolean.anker_manual_feed_in")
            manual_feed_in_target  = float(opts.get("manual_feed_in_target", 0.5))
            manual_feed_in_min_soc = float(opts.get("manual_feed_in_min_soc", 90.0))
            manual_feed_in_power   = float(opts.get("manual_feed_in_power", 800.0))

            feed_in_active = False
            if manual_feed_in_switch:
                feed_in_state = ha_get_state(manual_feed_in_switch, "off")
                feed_in_active = (feed_in_state == "on")

            grid_target = 10.0  # Kleiner Netzbezug-Sollwert
            is_actively_feeding_in = False

            if feed_in_active:
                if not state.get("manual_feed_in_active", False):
                    state["manual_feed_in_active"] = True
                    state["manual_feed_in_accumulated_kwh"] = 0.0
                    log.info("🔋 Manuelle Einspeisung gestartet. Ziel: %.2f kWh", manual_feed_in_target)

                # Überschuss prüfen
                surplus = (solar_p_hms + solix_pv) - haus_p
                conditions_met = (solix_soc >= manual_feed_in_min_soc) and (surplus > 0.0)
                if conditions_met:
                    is_actively_feeding_in = True
                    grid_target = -min(manual_feed_in_power, surplus)
                    if grid_p_raw < 0:
                        tick_kwh = (-grid_p_raw * TICK_S) / 3600000.0
                        state["manual_feed_in_accumulated_kwh"] = state.get("manual_feed_in_accumulated_kwh", 0.0) + tick_kwh

                accumulated = state.get("manual_feed_in_accumulated_kwh", 0.0)
                if accumulated >= manual_feed_in_target:
                    log.info("🔋 Manuelle Einspeisung Ziel von %.2f kWh erreicht! Schalte ab...", manual_feed_in_target)
                    if not DRY_RUN:
                        HA_SESSION.post(
                            f"{HA_URL}/api/services/input_boolean/turn_off",
                            json={"entity_id": manual_feed_in_switch},
                            timeout=5,
                        )
                    state["manual_feed_in_active"] = False
                    state["manual_feed_in_accumulated_kwh"] = 0.0
                    feed_in_active = False
                    is_actively_feeding_in = False
                    grid_target = 10.0

            # ------------------------------------------------------------------
            # 4. Zwangsladung (BMS Kalibrierung)
            # ------------------------------------------------------------------
            tage_seit = (time.time() - state["last_calibration_ts"]) / 86400
            sun_above = get_sun_state().get("state") == "above_horizon"

            # Kalibrierungs-Fälligkeit prüfen (Ziel: 10:00 Uhr des Ziel-Tages)
            try:
                last_cal_dt = datetime.fromtimestamp(state["last_calibration_ts"])
                target_dt = last_cal_dt + timedelta(days=calib_days)
                target_10am = target_dt.replace(hour=10, minute=0, second=0, microsecond=0)
                calibration_due = datetime.now() >= target_10am
            except Exception as e:
                log.error("Fehler bei Kalibrierungszeit-Berechnung: %s", e)
                calibration_due = tage_seit > calib_days

            zwangsladung_trigger = (
                calibration_due
                and not sun_above
                and solix_soc < 100
                and state["active_mode"] != "calibration"
                and not anker_smart_meter_active
            )
            if zwangsladung_trigger:
                log.info("Zwangsladung gestartet! %.1f Tage seit letzter Kalibrierung", tage_seit)
                state["active_mode"] = "calibration"
                save_state(state)

            if state["active_mode"] == "calibration" and solix_soc < 100:
                log.info("Zwangsladung läuft... SOC=%.1f%%", solix_soc)
                # Setpoint auf maximales Laden (negativ)
                write_holding_uint16(client, 10064, 3)  # Third-party control
                write_holding_int32(client, 10071, -int(solix_max_charge))
                ha_set_number(hms_2000_entity, 2000)    # Hoymiles voll offen halten
                state["last_hms_limit"] = 2000.0
                sleep_tick(TICK_S)
                continue

            if state["active_mode"] == "calibration" and solix_soc >= 100:
                log.info("Zwangsladung erfolgreich abgeschlossen!")
                state["last_calibration_ts"] = time.time()
                state["active_mode"] = "active"
                save_state(state)

            # ------------------------------------------------------------------
            # 5. Sonnenstand & Moduswechsel
            # ------------------------------------------------------------------
            if anker_smart_meter_active:
                state["active_mode"] = "smart_meter"
            elif not sun_above:
                state["active_mode"] = "night"
            else:
                state["active_mode"] = "active"

            # ------------------------------------------------------------------
            # 6. Regelungsalgorithmus (Nulleinspeisung)
            # ------------------------------------------------------------------
            # Grid Error berechnen
            grid_error = grid_p_raw - grid_target

            # Letzten Setpoint laden
            setpoint_last = float(state.get("last_setpoint", 0.0))

            # Neuen Setpoint berechnen (Dämpfung mit Faktor 0.5)
            setpoint_new = setpoint_last + grid_error * 0.5

            # SOC-Grenzen anwenden
            if low_soc_active:
                # Entladen stoppen: Setpoint darf nicht positiv sein
                setpoint_new = min(0.0, setpoint_new)
            elif solix_soc >= soc_normal_max:
                # Akku voll: Laden stoppen, Setpoint darf nicht negativ sein
                setpoint_new = max(0.0, setpoint_new)

            # Grenzwerte einhalten (max_charge bis max_discharge)
            setpoint_new = max(-solix_max_charge, min(solix_max_discharge, setpoint_new))
            
            # Runden auf 10W Schritte zur Schonung
            setpoint_new_rounded = round(setpoint_new / 10.0) * 10.0

            # ------------------------------------------------------------------
            # 6b. Hoymiles-Drosselung (wenn Akku voll oder Ladung blockiert)
            # ------------------------------------------------------------------
            hms_limit_last = float(state.get("last_hms_limit", 2000.0))
            hms_limit_new = hms_limit_last

            # Wenn wir einspeisen (grid_error < -50) und der Akku nicht mehr laden kann
            # (weil er voll ist oder das Setpoint-Limit erreicht hat), müssen wir den Hoymiles drosseln
            if grid_error < -50:
                # Kann der Akku noch mehr Ladeleistung aufnehmen?
                if anker_smart_meter_active:
                    akku_kann_laden = (solix_soc < soc_normal_max)
                else:
                    akku_kann_laden = (solix_soc < soc_normal_max) and (setpoint_new_rounded > -solix_max_charge)
                if not akku_kann_laden:
                    # Drosselung erforderlich
                    hms_limit_new = hms_limit_last + grid_error * 0.5
            elif grid_error > 50:
                # Netzbezug: Hoymiles freigeben
                hms_limit_new = hms_limit_last + grid_error * 0.5

            hms_limit_new = max(0.0, min(2000.0, hms_limit_new))
            hms_limit_new_rounded = round(hms_limit_new / 10.0) * 10.0

            # ------------------------------------------------------------------
            # 7. Hardware ansteuern (Modbus & HA)
            # ------------------------------------------------------------------
            if not anker_smart_meter_active:
                # 1. Sicherstellen, dass Operating Mode auf 3 (Third-Party Control) steht
                current_mode = read_holding_uint16(client, 10064)
                if current_mode != 3:
                    log.info("Setze Betriebsmodus auf Third-Party Control (Mode 3)...")
                    write_holding_uint16(client, 10064, 3)

                # 2. Setpoint über Modbus an den Anker schreiben
                write_holding_int32(client, 10071, int(setpoint_new_rounded))
            else:
                # Im Smart-Meter-Modus regelt die Solarbank über den Anker Smart Meter.
                # Wir lesen nur Werte und überspringen das Senden von Sollwerten.
                pass

            # 3. Hoymiles limit über HA setzen (nur bei Änderungen >= 50W)
            if hms_online:
                if abs(hms_limit_last - hms_limit_new_rounded) >= 50:
                    ha_set_number(hms_2000_entity, hms_limit_new_rounded)
                    state["last_hms_limit"] = hms_limit_new_rounded

            log.info("Modus=%s SOC=%.1f%% | Setpoint=%dW output=%.1fW | grid=%.0fW haus=%.0fW HMS-Limit=%dW",
                     state["active_mode"], solix_soc, setpoint_new_rounded, solix_ac_output,
                     grid_p_raw, haus_p, hms_limit_new_rounded)

            # ------------------------------------------------------------------
            # 8. Logs und State speichern
            # ------------------------------------------------------------------
            csv_log({
                "ts":        time.strftime("%Y-%m-%d %H:%M:%S"),
                "mode":      state["active_mode"],
                "soc":       round(solix_soc, 1),
                "grid_p":    round(grid_p_raw, 1),
                "haus_p":    round(haus_p, 1),
                "solar_p":   round(solix_pv + (solar_p_hms if hms_online else 0.0), 1),
                "setpoint":  round(setpoint_new_rounded, 0),
                "battery_p": round(solix_battery_p, 1),
                "pv":        round(solix_pv, 1),
                "load_p":    round(solix_load_p, 1),
                "hms_limit": round(hms_limit_new_rounded, 0),
                "hms_power": round(solar_p_hms, 1),
                "hms_online": 1 if hms_online else 0
            })

            # Virtuelle Sensoren an HA pushen
            ha_push_sensor("sensor.anker_hausverbrauch", haus_p, "W", "power", "Hausverbrauch (Anker Controller)")
            ha_push_sensor("sensor.anker_grid_p", grid_p_raw, "W", "power", "Netz aktuell (Anker Controller)")
            ha_push_sensor("sensor.anker_solar_p", solix_pv + (solar_p_hms if hms_online else 0.0), "W", "power", "Solar gesamt (Anker Controller)")
            ha_push_sensor("sensor.anker_battery_ac", solix_ac_output, "W", "power", "Batterie AC (Anker Controller)")
            ha_push_sensor("sensor.anker_solix_soc", solix_soc, "%", "battery", "Anker Solix SOC")
            ha_push_sensor("sensor.anker_solix_pv", solix_pv, "W", "power", "Anker Solix PV Leistung")
            ha_push_sensor("sensor.anker_solix_battery_power", solix_battery_p, "W", "power", "Anker Solix Batterie Leistung")
            ha_push_sensor("sensor.anker_solix_load_power", solix_load_p, "W", "power", "Anker Solix Last Leistung")
            ha_push_sensor("sensor.anker_solix_setpoint", setpoint_new_rounded, "W", "power", "Anker Solix Sollwert")

            state["grid_p_filtered"] = grid_p_raw
            state["solar_p_last"]    = solar_p_hms
            state["haus_p_last"]     = haus_p
            state["last_setpoint"]   = setpoint_new_rounded
            state["pv_last"]         = solix_pv
            save_state_throttled(state)

        except Exception as e:
            log.error("Fehler im Regelzyklus: %s", e, exc_info=True)

        elapsed = time.monotonic() - tick_start
        sleep_tick(TICK_S - elapsed)

    # Shutdown-Safe-State
    log.info("Shutdown — übergebe an Geräte-Selbstregelung...")
    try:
        if not anker_smart_meter_active:
            # Betriebsmodus auf Self-Consumption (0) setzen, damit das Gerät selbst regelt
            write_holding_uint16(client, 10064, 0)
        ha_set_number(hms_2000_entity, 2000)
    except Exception as e:
        log.error("Fehler beim Shutdown-Safe-State: %s", e)
    log.info("Controller beendet.")

if __name__ == "__main__":
    main()
