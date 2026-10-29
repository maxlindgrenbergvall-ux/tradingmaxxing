# Momentum-burst-bot (Alpaca paper trading)

Byggd på idéerna i [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills):

| Skill i repot | Hur den används här |
|---|---|
| `stockbee-momentum-burst-screener` | Intradag-variant: prisutbrott + volym- och range-expansion + stängning nära toppen, filter mot överutsträckta rörelser (`bot/strategy.py`) |
| `position-sizer` | ATR-baserad storlek: `konto × risk% / stoppavstånd`, med tak (`bot/sizing.py`) |
| `drawdown-circuit-breaker` | Daglig förlustgräns + förlust-streak-paus, plus **hård nödbroms vid −25 %** (`bot/risk.py`) |
| `portfolio-manager` (Alpaca-koppling) | Samma Alpaca-API och paper-endpoint (`bot/broker.py`) |

> ⚠️ Inte finansiell rådgivning. Snabba trades i volatila instrument kan förlora pengar snabbt. Repot självt är uttryckligen *inte* byggt för automatisk handel – det här är ett experiment att köra på demokonto.

## Säkerhetsspärrar
1. **Nödbroms 25 %** – om equity (inkl. orealiserat) ≤ 75 % av startkapitalet: alla ordrar annulleras, alla positioner stängs, `state/HALTED` skrivs och processen avslutas. Den vägrar starta igen tills du kört `unlock` manuellt.
2. **Demovecka** – `mode = "paper"` som standard. `live` spärras tills 7 dagar gått sedan första demokörningen *och* `BOT_ALLOW_LIVE=JA` är satt.
3. Daglig förlustgräns (8 %), paus 60 min efter 4 förluster i rad, max 3 positioner, 1 % risk/trade, max 20 % av kontot per position.
4. 10 fel i rad i loopen → stopp för granskning.

## Programmet med dashboard (enklast)
1. Installera Python från <https://www.python.org/downloads/> (Windows: bocka i **Add python.exe to PATH**).
2. Packa upp mappen och dubbelklicka på
   - **Mac:** `Starta Momentum Bot.command` (första gången: högerklicka → **Öppna** → **Öppna**)
   - **Windows:** `Starta Momentum Bot.bat` (om SmartScreen varnar: **Mer info** → **Kör ändå**)
3. Första starten installerar allt (cirka en minut). Sedan öppnas dashboarden.
4. Prova **Simulering** → **Starta** direkt, eller klicka på kugghjulet och klistra in dina Alpaca Paper-nycklar och kör **Demokonto**.

**Tempo:** välj *Normal* eller *Hög frekvens* uppe i menyraden (när boten är stoppad). Hög frekvens är förvalt: lösare instegskrav, max 8 min per trade, upp till 6 positioner, 11 krypton, kontroll var 10:e sekund och 0,4 % risk per trade. Nödbromsen vid −25 % och dagsgränsen 8 % gäller i båda lägena. Inställningarna finns under `[profiles.hogfrekvens]` i `config.toml`.

**Live-positioner:** varje öppen position får ett eget kort med resultat, R-multipel, ett spår från stopp till mål, tid kvar till tidsstoppet och ett live-diagram. Växla mellan **TradingView** (TradingViews officiella livediagram) och **Botens vy** (ljusstakar med inköp, stopp och mål inritade, byggt med TradingViews öppna bibliotek Lightweight Charts). Klicka på en rad under Öppna positioner för att hoppa till dess diagram. Utan öppna positioner kan du bevaka valfri symbol från bevakningslistan.

Dashboarden visar kontovärde, avstånd till nödbromsen, demoveckans dagar, resultat idag och totalt, vinstfrekvens, profit factor, snitt per trade, största drawdown, en kurva över kontovärdet, resultat per trade, öppna positioner, tradehistorik och logg. Boten kör så länge programfönstret (eller terminalfönstret) är öppet, och datorn hindras från att somna.

## Kom igång (kommandorad)
1. Skapa ett gratis konto på <https://alpaca.markets>, gå till **Paper Trading** och generera API-nycklar.
2. Installera och starta:
```bash
pip install -r requirements.txt
export ALPACA_API_KEY="din-paper-key-id"
export ALPACA_SECRET_KEY="din-paper-secret"
python -m bot run
```
Standard är krypto (BTC, ETH, SOL, DOGE, AVAX, LINK) som handlas dygnet runt – bra för att få många trades under demoveckan. Byt till `asset_class = "stocks"` i `config.toml` för TSLA, NVDA, COIN, SOXL m.fl. (bara under US-börsens öppettider).

## Låt den ticka dygnet runt
Alternativ A – på din egen dator (Mac/Linux), lämna terminalen öppen:
```bash
./start.sh
```
Alternativ B – Docker (t.ex. på en billig VPS eller gratis Oracle Cloud-VM):
```bash
docker compose up -d --build
docker compose logs -f
```
`start.sh` startar om boten vid krasch eller nätverksfel, men **inte** efter nödbromsen.

## Kommandon
```bash
python -m bot status                                   # nödbromsnivå, drawdown, demovecka
python -m bot sim --minutes 2000                       # offline-test, inga nycklar
python -m bot unlock --confirm GRANSKAT                # efter manuell granskning
python -m bot unlock --confirm GRANSKAT --reset-baseline   # fortsätt med nuvarande kapital som ny bas
python -m pytest -q                                    # tester
```

## Filer i `state/`
- `risk_state.json` – startkapital, stoppstatus, händelselogg
- `open_trades.json` – öppna trades med stopp/mål (överlever omstart)
- `journal.jsonl` – alla stängda trades med P&L och exit-orsak (för granskning, jfr `trader-memory-core`)
- `bot.log`, `HALTED`

## Begränsningar att känna till
- Stopp/mål hanteras av boten (polling var 30:e sekund), inte som serverorder hos Alpaca. **Om processen inte kör skyddas inte positionerna** – kör den på en server/VPS med t.ex. `systemd` eller `tmux`. Nödbromsen kontrolleras dock direkt vid nästa start.
- Strategin är inte backtestad mot riktig historik. Använd demoveckan för att utvärdera `journal.jsonl` innan du ens överväger live.
