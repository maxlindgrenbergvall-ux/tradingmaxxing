#!/usr/bin/env bash
# Håller boten igång dygnet runt. Startar om vid krasch/nätverksfel,
# men stannar för gott om nödbromsen löst ut (exit 3) eller live är spärrat (exit 4).
cd "$(dirname "$0")"
while true; do
  python3 -m bot run
  code=$?
  if [ "$code" -eq 3 ] || [ "$code" -eq 4 ] || [ "$code" -eq 2 ]; then
    echo "Boten stoppad (kod $code) - kräver manuell granskning. Startar INTE om."
    exit "$code"
  fi
  echo "Boten avslutades (kod $code) - startar om om 30 s..."
  sleep 30
done
