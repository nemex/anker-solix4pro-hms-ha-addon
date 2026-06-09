#!/usr/bin/env python3
"""
Anker Solix 4 Pro Controller v1.0.0
===================================
Template für Nulleinspeisung mit Anker Solix 2/3/4 Pro + Hoymiles HMS.
"""

import json
import logging
import os
import signal
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("anker_solix_controller")

OPTIONS_PATH = "/data/options.json"
STATE_PATH   = "/data/controller_state.json"
TICK_S       = 5
RUNNING      = True

def _handle_term(signum, frame):
    global RUNNING
    log.info("Signal %s empfangen — beende...", signum)
    RUNNING = False

def main():
    global RUNNING
    log.info("Anker Solix 4 Pro Controller v1.0.0 startet...")
    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT, _handle_term)

    # Hier wird die eigentliche Regelungslogik implementiert
    while RUNNING:
        try:
            log.info("Regelzyklus läuft (Dummy)...")
            time.sleep(TICK_S)
        except Exception as e:
            log.error("Fehler im Regelzyklus: %s", e)

if __name__ == "__main__":
    main()
