#!/usr/bin/env bash
echo "Starting Anker Solix 4 Pro Controller..."

python3 /app/controller.py &
PID_CTRL=$!

python3 /app/web_ui.py &
PID_UI=$!

# SIGTERM/SIGINT abfangen und an die Python-Prozesse weiterleiten
term_handler() {
  echo "Stop-Signal empfangen — beende Prozesse sauber..."
  kill -TERM "$PID_CTRL" "$PID_UI" 2>/dev/null
  wait
  echo "Sauber beendet."
  exit 0
}
trap term_handler TERM INT

# Beenden, sobald EINER der beiden Prozesse stirbt
wait -n
echo "Ein Prozess wurde beendet — fahre Container herunter, damit ein Neustart erfolgt."
kill "$PID_CTRL" "$PID_UI" 2>/dev/null
exit 1
