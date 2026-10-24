# Angebote

Private App für die Familie: zeigt die aktuellen Prospekt-Angebote der Supermärkte
in Emsdetten/Steinfurt, Winsen (Luhe) und Hamburg-Harburg, sortiert nach Grundpreis.

- **App:** `docs/` – wird über GitHub Pages ausgeliefert. Vorlieben und Wochenliste
  speichert jedes Handy für sich.
- **Sammler:** `sammler.py` – läuft jeden Morgen über GitHub Actions
  (`.github/workflows/sammeln.yml`) und schreibt die Angebote nach `docs/data/`.
- **Cache:** `cache/` – bereits per KI ausgelesene K+K-Prospekte und Produktkategorien,
  damit nichts doppelt bezahlt wird.

## Quellen

| Märkte | Quelle |
|---|---|
| Lidl, Penny, Netto, Edeka, Kaufland, Rewe, famila, Combi, budni, … | marktguru (alle Angebote pro PLZ) |
| Aldi | aldi-nord.de |
| Rossmann | kaufDA (Einzelangebote) |
| K+K | kaufDA-Prospektbilder, ausgelesen mit Claude Haiku |

Alle Daten stammen aus öffentlich zugänglichen Prospekten. Nur für den privaten Gebrauch.

## Einrichtung

1. Repository-Secret `ANTHROPIC_API_KEY` anlegen (Settings → Secrets and variables → Actions).
2. GitHub Pages aktivieren: Settings → Pages → Branch `main`, Ordner `/docs`.
3. Unter Actions → „Angebote sammeln“ → „Run workflow“ einmal manuell starten.

## Orte ändern

In `sammler.py` die Liste `REGIONEN` anpassen (Name, PLZs, ob es K+K gibt).

## Lokal ausführen

```
pip install -r requirements.txt
python sammler.py
cd docs && python -m http.server
```
