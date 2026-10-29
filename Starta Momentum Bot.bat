@echo off
rem Windows: dubbelklicka for att starta Momentum Bot.
cd /d "%~dp0"
chcp 65001 >nul
where py >nul 2>nul
if errorlevel 1 (
  echo Python saknas. Installera fran https://www.python.org/downloads/ och bocka i "Add python.exe to PATH".
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Forsta start: installerar, tar nagon minut...
  py -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -q --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -q pywebview || echo Inget eget fonster - dashboarden oppnas i webblasaren.
)
rem Kontrollera beroenden vid varje start (snabbt om allt redan finns)
".venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt
".venv\Scripts\python.exe" -m bot.app
pause
