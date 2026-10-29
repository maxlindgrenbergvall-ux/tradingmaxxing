#!/bin/bash
# Mac: dubbelklicka för att starta Momentum Bot.
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python saknas. Installera från https://www.python.org/downloads/ och försök igen."
  read -n 1 -s -r -p "Tryck på valfri tangent för att stänga..."; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "Första start: installerar (tar någon minut)..."
  python3 -m venv .venv || { echo "Kunde inte skapa miljö."; read -n 1 -s -r; exit 1; }
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -q pywebview || echo "(Inget eget fönster – dashboarden öppnas i webbläsaren.)"
fi
while true; do
  # Kontrollera beroenden vid varje start (snabbt om allt redan finns)
  .venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
  # caffeinate hindrar Macen från att somna medan boten kör
  caffeinate -i .venv/bin/python -m bot.app
  # Kod 10 = programmet har uppdaterat sig och vill startas om
  [ $? -eq 10 ] || break
done
