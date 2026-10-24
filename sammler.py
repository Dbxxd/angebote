#!/usr/bin/env python3
"""
Sammler für die Prospekt-App.

Holt ALLE aktuellen Angebote der relevanten Märkte für jede Region und schreibt
sie nach docs/data/<region>.json. Die Web-App in docs/ lädt diese Dateien.

Quellen:
  - marktguru     alle Angebote pro PLZ (Lidl, Penny, Netto, Edeka, Kaufland, Rewe, budni, Combi, ...)
  - aldi-nord.de  bundesweit gleiche Angebote
  - kaufDA        Rossmann (fertige Einzelangebote, bundesweit)
                  K+K (nur Prospektbilder -> per Claude-API ausgelesen, mit Cache)

Nutzung:
    python sammler.py                 # alle Regionen
    python sammler.py --region steinfurt

Benötigt: pip install requests anthropic
K+K braucht die Umgebungsvariable ANTHROPIC_API_KEY (sonst wird K+K übersprungen).
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASIS = Path(__file__).resolve().parent
DATEN_DIR = BASIS / "docs" / "data"
CACHE_DIR = BASIS / "cache"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# -----------------------------------------------------------------------------
# Regionen
# -----------------------------------------------------------------------------
# plz:  PLZs, für die marktguru abgefragt wird (Ergebnisse werden zusammengeführt)
# kk:   ob es dort K+K gibt
REGIONEN = {
    "steinfurt": {
        "name": "Emsdetten / Steinfurt",
        "plz": ["48282", "48341", "48356", "48565"],
        "kk": True,
    },
    "winsen": {
        "name": "Winsen (Luhe)",
        "plz": ["21423"],
        "kk": False,
    },
    "harburg": {
        "name": "Hamburg-Harburg",
        "plz": ["21073"],
        "kk": False,
    },
}

# -----------------------------------------------------------------------------
# Märkte: Kurzname -> Muster im Händlernamen. Alles andere (Möbel, Baumarkt, ...)
# wird verworfen.
# -----------------------------------------------------------------------------
MAERKTE = {
    "Lidl": ["lidl"],
    "Aldi": ["aldi"],
    "Penny": ["penny"],
    "Netto": ["netto marken"],
    "Rewe": ["rewe"],  # vor Edeka prüfen, sonst passt "REWE Center" auf "e center"
    "Edeka": ["edeka", "e center", "e xpress"],
    "Kaufland": ["kaufland"],
    "K+K": ["klaas", "k+k"],
    "Combi": ["combi"],
    "famila": ["famila"],
    "Norma": ["norma"],
    "nahkauf": ["nahkauf"],
    "Rossmann": ["rossmann"],
    "budni": ["budni"],
    "dm": ["dm-drogerie"],
}
AUSSCHLIESSEN = ["foodservice"]  # Großhandel, nicht für Endkunden

# Diese Märkte kommen aus eigenen Quellen; marktguru-Treffer dafür werden ignoriert,
# damit nichts doppelt auftaucht.
EIGENE_QUELLE = {"Aldi", "Rossmann", "K+K"}


def markt_kurzname(name: str) -> str | None:
    low = (name or "").lower()
    if any(a in low for a in AUSSCHLIESSEN):
        return None
    for kurz, muster in MAERKTE.items():
        if any(m in low for m in muster):
            return kurz
    return None


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# -----------------------------------------------------------------------------
# Hilfsfunktionen
# -----------------------------------------------------------------------------

def zahl(x):
    if x is None or x == "":
        return None
    try:
        return round(float(str(x).replace(",", ".")), 2)
    except ValueError:
        return None


def grundpreis_berechnen(preis: float, packung: str) -> tuple[float | None, str]:
    """Grundpreis aus Mengenangaben wie '100 g', '1-kg-Schale', '4x0,44-L-Dose'.
    Bei unklaren Angaben (Spannen, 'x für y', gemischte Größen) wird nichts geraten."""
    p = (packung or "").lower()
    z = r"(\d+(?:[.,]\d+)?)"
    e = r"\s*-?\s*(kg|g|ml|l)\b"

    multi = re.fullmatch(rf".*?(\d+)\s*[x×]\s*{z}{e}[^\d]*", p)
    if multi and not re.search(r"/|\+|\bbis\b|für", p):
        menge = int(multi.group(1)) * float(multi.group(2).replace(",", "."))
        einheit = multi.group(3)
    else:
        if re.search(r"\d\s*[x×/+–-]\s*\d|(kg|g|ml|l)\s*[–-]\s*\d|\bbis\b|\bfür\b|stück|stk", p):
            return None, ""
        m = re.search(z + e, p)
        if not m:
            return None, ""
        menge = float(m.group(1).replace(",", "."))
        einheit = m.group(2)
    if einheit == "g":
        menge, einheit = menge / 1000, "kg"
    elif einheit == "ml":
        menge, einheit = menge / 1000, "l"
    if menge <= 0:
        return None, ""
    return round(preis / menge, 2), einheit


def angebot(markt, marke, name, preis, alt_preis, gp, ge, packung, von, bis, kategorie, quelle):
    """Einheitliches, kompaktes Format (kurze Schlüssel halten die JSON-Datei klein)."""
    preis, alt_preis, gp = zahl(preis), zahl(alt_preis), zahl(gp)
    if alt_preis is not None and preis is not None and alt_preis <= preis:
        alt_preis = None
    ge = ge if ge in ("kg", "l") else ""
    if gp is None or not ge:
        if preis:
            gp, ge = grundpreis_berechnen(preis, packung)
        else:
            gp, ge = None, ""
    return {
        "h": markt,
        "m": (marke or "").strip(),
        "n": (name or "").strip(),
        "p": preis,
        "ap": alt_preis,
        "gp": gp,
        "ge": ge,
        "pk": (packung or "").strip(),
        "von": von or "",
        "bis": bis or "",
        "k": (kategorie or "").strip(),
        "q": quelle,
    }


def doppelte_entfernen(angebote: list[dict]) -> list[dict]:
    gesehen, ergebnis = set(), []
    for a in angebote:
        s = (a["h"], a["m"].lower(), a["n"].lower(), a["p"], a["von"], a["pk"].lower())
        if s not in gesehen:
            gesehen.add(s)
            ergebnis.append(a)
    return ergebnis


# =============================================================================
# Quelle: marktguru
# =============================================================================

MG_API = "https://api.marktguru.de/api/v1"
_mg_session = None
MG_HAENDLER_DIAGNOSE: dict[str, dict[str, int]] = {}  # PLZ -> {Händlername: Anzahl}


def mg_session() -> requests.Session:
    global _mg_session
    if _mg_session:
        return _mg_session
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    html = s.get("https://www.marktguru.de", timeout=20).text
    ak = re.search(r'"apiKey"\s*:\s*"([^"]+)"', html)
    ck = re.search(r'"clientKey"\s*:\s*"([^"]+)"', html)
    if not ak or not ck:
        raise RuntimeError("marktguru: API-Keys nicht gefunden (Seite geändert?)")
    s.headers.update({"x-apikey": ak.group(1), "x-clientkey": ck.group(1)})
    _mg_session = s
    return s


def mg_packung(o: dict) -> str:
    vol = o.get("volume")
    einheit = (o.get("unit") or {}).get("shortName") or ""
    anzahl = o.get("quantity") or 1
    if not isinstance(vol, (int, float)):
        return ""
    if einheit == "kg" and vol < 1:
        txt = f"{vol * 1000:g} g"
    elif einheit == "l" and vol < 1:
        txt = f"{vol * 1000:g} ml"
    else:
        txt = f"{vol:g} {einheit}".replace(".", ",")
    if anzahl and anzahl > 1:
        txt = f"{anzahl:g} x {txt}"
    return txt


def mg_alle_angebote(plz: str) -> list[dict]:
    """Alle Angebote einer PLZ, seitenweise (512 pro Abruf)."""
    s = mg_session()
    roh, offset, gesamt = [], 0, None
    while gesamt is None or offset < gesamt:
        r = s.get(
            f"{MG_API}/offers",
            params={"as": "web", "zipCode": plz, "limit": 1000, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        j = r.json()
        gesamt = j.get("totalResults", 0)
        teil = j.get("results") or []
        if not teil:
            break
        roh.extend(teil)
        offset += len(teil)
        time.sleep(0.3)  # höflich bleiben

    # Diagnose: welche Händler liefert marktguru für diese PLZ insgesamt?
    zaehler: dict[str, int] = {}
    for o in roh:
        n = (o.get("advertisers") or [{}])[0].get("name", "?")
        zaehler[n] = zaehler.get(n, 0) + 1
    MG_HAENDLER_DIAGNOSE[plz] = dict(sorted(zaehler.items(), key=lambda x: -x[1]))

    ergebnis = []
    for o in roh:
        markt = markt_kurzname((o.get("advertisers") or [{}])[0].get("name", ""))
        if not markt or markt in EIGENE_QUELLE:
            continue
        v = (o.get("validityDates") or [{}])[0]
        einheit = (o.get("unit") or {}).get("shortName") or ""
        kat = (o.get("categories") or [{}])[0].get("name", "")
        ergebnis.append(angebot(
            markt,
            (o.get("brand") or {}).get("name"),
            (o.get("product") or {}).get("name"),
            o.get("price"), o.get("oldPrice"),
            o.get("referencePrice"), einheit,
            mg_packung(o),
            (v.get("from") or "")[:10], (v.get("to") or "")[:10],
            kat, "marktguru",
        ))
    return ergebnis


# =============================================================================
# Quelle: Aldi Nord
# =============================================================================

ALDI_SEITEN = [
    "https://www.aldi-nord.de/angebote.html",
    "https://www.aldi-nord.de/angebote-vorschau.html",
]


def aldi_datum(ts) -> str:
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().date().isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def aldi_alle_angebote() -> list[dict]:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "de-DE,de;q=0.9"})
    produkte = {}
    for url in ALDI_SEITEN:
        r = s.get(url, timeout=30)
        r.raise_for_status()
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
        if not m:
            continue
        api = json.loads(m.group(1)).get("props", {}).get("pageProps", {}).get("apiData")
        if isinstance(api, str):
            api = json.loads(api)

        def suche(o):
            if isinstance(o, dict):
                if isinstance(o.get("algoliaDataMap"), dict):
                    for pid, p in o["algoliaDataMap"].items():
                        produkte.setdefault(pid, p)
                for v in o.values():
                    suche(v)
            elif isinstance(o, list):
                for v in o:
                    suche(v)

        suche(api)

    if not produkte:
        raise RuntimeError("Aldi: keine Produktdaten gefunden (Seitenaufbau geändert?)")

    ergebnis = []
    for p in produkte.values():
        cp = p.get("currentPrice") or {}
        bp = (cp.get("basePrice") or [{}])[0]
        ergebnis.append(angebot(
            "Aldi",
            (p.get("brandName") or "").title(),
            p.get("name"),
            cp.get("priceValue"),
            (cp.get("strikePrice") or {}).get("strikePriceValue"),
            bp.get("basePriceValue"), bp.get("basePriceScale"),
            p.get("salesUnit"),
            aldi_datum(cp.get("validFrom")), aldi_datum(cp.get("validUntil")),
            "", "aldi-nord.de",
        ))
    return ergebnis


# =============================================================================
# Quelle: kaufDA (Rossmann direkt, K+K über Bilder + Claude)
# =============================================================================

KAUFDA_SEITEN = {
    "Rossmann": "https://www.kaufda.de/Emsdetten/Rossmann/p-r98",
    "K+K": "https://www.kaufda.de/Emsdetten/K-K-Klaas-Kock/p-r586",
}
KAUFDA_PAGES_API = "https://content-viewer-be.kaufda.de/v1/brochures/{id}/pages"
KAUFDA_LATLNG = {"lat": "52.1758", "lng": "7.52741"}  # Emsdetten


def kaufda_datum(s: str) -> str:
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone().date().isoformat()
    except ValueError:
        return s[:10]


def kaufda_prospekte(seite: str) -> list[dict]:
    r = requests.get(seite, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("kaufDA: keine Seitendaten gefunden")
    d = json.loads(m.group(1))
    viewer = d["props"]["pageProps"]["pageInformation"]["brochures"].get("viewer") or []
    return [
        {
            "id": b["contentId"],
            "titel": b.get("title", ""),
            "von": kaufda_datum(b.get("validFrom", "")),
            "bis": kaufda_datum(b.get("validUntil", "")),
        }
        for b in viewer
    ]


def kaufda_seiten(prospekt_id: str) -> list[dict]:
    r = requests.get(
        KAUFDA_PAGES_API.format(id=prospekt_id),
        params={"partner": "kaufda_web", **KAUFDA_LATLNG},
        headers={"User-Agent": UA},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("contents", [])


def rossmann_alle_angebote() -> list[dict]:
    ergebnis = []
    for prospekt in kaufda_prospekte(KAUFDA_SEITEN["Rossmann"]):
        for seite in kaufda_seiten(prospekt["id"]):
            for o in seite.get("offers", []):
                c = o.get("content", {})
                p = (c.get("products") or [{}])[0]
                preise = {d.get("type"): d.get("min") for d in (c.get("deals") or [])}
                gueltig = ((c.get("publicationProfiles") or [{}])[0].get("validity") or {})
                beschreibung = " ".join(
                    x.get("paragraph", "") for x in (p.get("description") or [])
                ).strip()
                kats = [k.get("name", "") for k in (p.get("categoryPaths") or [])]
                ergebnis.append(angebot(
                    "Rossmann",
                    p.get("brandName"), p.get("name"),
                    preise.get("SALES_PRICE") or None, preise.get("REGULAR_PRICE") or None,
                    None, "",
                    beschreibung[:80],
                    kaufda_datum(gueltig.get("startDate", "")) or prospekt["von"],
                    kaufda_datum(gueltig.get("endDate", "")) or prospekt["bis"],
                    kats[-1] if kats else "", "kaufDA",
                ))
    return ergebnis


# --- K+K ----------------------------------------------------------------------

KK_MODELL = os.environ.get("PROSPEKT_MODELL", "claude-haiku-5-5")

KK_PROMPT = """Das ist eine Seite aus einem Supermarkt-Prospekt.
Erfasse ALLE Angebote auf dieser Seite, die einen Preis haben.

Antworte NUR mit einem JSON-Array, ohne weiteren Text. Jedes Element:
{
  "marke": "Markenname oder leer",
  "name": "Produktname, kurz",
  "packung": "Packungsgröße wie gedruckt, z.B. '250-g-Packung' oder '20 x 0,5 l'",
  "preis": Angebotspreis in Euro als Zahl,
  "alt_preis": durchgestrichener/vorheriger Preis als Zahl oder null,
  "grundpreis": Preis pro kg bzw. l als Zahl oder null,
  "gp_einheit": "kg" oder "l" oder "",
  "kategorie": "ein allgemeines deutsches Wort für die Produktart, z.B. Butter, Bier, Kaffee, Käse, Obst, Fleisch"
}

Regeln:
- Preise mit Punkt als Dezimaltrennzeichen (1.99, nicht 1,99).
- Steht ein Grundpreis z.B. als "1 kg = 7.96" auf der Seite, übernimm ihn. Sonst null.
- Gilt ein Preis für mehrere Sorten, nur EIN Eintrag.
- Erfinde nichts. Was unlesbar ist, weglassen.
- Keine Seite ohne Angebote? Dann [].
"""


def kk_seite_auslesen(client, bild_url: str) -> tuple[list[dict], int, int]:
    r = requests.get(bild_url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    media_type = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
    if media_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        media_type = "image/jpeg"
    antwort = client.messages.create(
        model=KK_MODELL,
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": media_type,
                    "data": base64.standard_b64encode(r.content).decode("ascii"),
                }},
                {"type": "text", "text": KK_PROMPT},
            ],
        }],
    )
    text = "".join(b.text for b in antwort.content if getattr(b, "type", "") == "text")
    a, b = text.find("["), text.rfind("]")
    try:
        daten = json.loads(text[a:b + 1]) if a >= 0 and b > a else []
    except json.JSONDecodeError:
        log("    (Antwort war kein gültiges JSON, Seite übersprungen)")
        daten = []
    return daten, antwort.usage.input_tokens, antwort.usage.output_tokens


def kk_prospekt_auslesen(prospekt: dict) -> list[dict]:
    cache = CACHE_DIR / f"kk_{prospekt['id']}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))["angebote"]

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY fehlt – K+K übersprungen")
    import anthropic

    client = anthropic.Anthropic()
    bilder = []
    for seite in kaufda_seiten(prospekt["id"]):
        b = {i.get("size"): i.get("url") for i in seite.get("images", [])}
        url = b.get("1600x1600") or (list(b.values())[-1] if b else None)
        if url:
            bilder.append(url)

    log(f"  K+K: lese '{prospekt['titel']}' ({len(bilder)} Seiten) mit {KK_MODELL} aus ...")
    alle, ti, to = [], 0, 0
    for i, url in enumerate(bilder, 1):
        daten, a, b = kk_seite_auslesen(client, url)
        log(f"    Seite {i}: {len(daten)} Angebote")
        alle.extend(daten)
        ti, to = ti + a, to + b
    log(f"  K+K: {len(alle)} Angebote, {ti} Tokens rein / {to} raus")

    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_text(
        json.dumps({"prospekt": prospekt, "modell": KK_MODELL, "angebote": alle},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return alle


def kk_alle_angebote() -> list[dict]:
    ergebnis = []
    for prospekt in kaufda_prospekte(KAUFDA_SEITEN["K+K"]):
        for a in kk_prospekt_auslesen(prospekt):
            ergebnis.append(angebot(
                "K+K", a.get("marke"), a.get("name"),
                a.get("preis"), a.get("alt_preis"),
                a.get("grundpreis"), a.get("gp_einheit"),
                a.get("packung"),
                prospekt["von"], prospekt["bis"],
                a.get("kategorie"), "kaufDA+KI",
            ))
    # Titelseite und Innenteil zeigen oft dasselbe Angebot
    gesehen, eindeutig = set(), []
    for a in ergebnis:
        w = re.findall(r"[a-zäöüß]+", a["n"].lower())
        s = (a["p"], w[0] if w else a["n"])
        if s not in gesehen:
            gesehen.add(s)
            eindeutig.append(a)
    return eindeutig


# =============================================================================
# Fehlende Kategorien per Claude ergänzen (v.a. Aldi liefert keine)
# =============================================================================

KAT_PROMPT = """Ordne jedem Supermarkt-Produkt eine Kategorie zu: ein allgemeines deutsches Wort,
wie man im Prospekt danach suchen würde (z.B. Butter, Käse, Kaffee, Bier, Wein, Obst, Gemüse,
Fleisch, Wurst, Fisch, Brot, Süßigkeiten, Getränke, Drogerie, Haushalt, Kleidung, Elektronik).
"Jacobs Krönung" -> Kaffee, "Milsani Markenbutter" -> Butter, "Krombacher Pils" -> Bier.

Antworte NUR mit einem JSON-Objekt {"Nummer": "Kategorie", ...}.

Produkte:
"""


def kategorien_ergaenzen(angebote: list[dict]) -> None:
    """Setzt a['k'] für Angebote ohne Kategorie. Ergebnisse werden dauerhaft gecacht."""
    cache_datei = CACHE_DIR / "kategorien.json"
    cache = json.loads(cache_datei.read_text(encoding="utf-8")) if cache_datei.exists() else {}
    schluessel = lambda a: f"{a['m']}|{a['n']}".lower()

    offen = sorted({schluessel(a) for a in angebote if not a["k"] and schluessel(a) not in cache})
    if offen and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            import anthropic
            client = anthropic.Anthropic()
            for start in range(0, len(offen), 150):
                teil = offen[start:start + 150]
                liste = "\n".join(f"{i}: {s.replace('|', ' ')}" for i, s in enumerate(teil))
                antwort = client.messages.create(
                    model=KK_MODELL, max_tokens=6000,
                    messages=[{"role": "user", "content": KAT_PROMPT + liste}],
                )
                text = "".join(b.text for b in antwort.content if getattr(b, "type", "") == "text")
                a, b = text.find("{"), text.rfind("}")
                zuordnung = json.loads(text[a:b + 1]) if a >= 0 and b > a else {}
                for i, s in enumerate(teil):
                    k = zuordnung.get(str(i))
                    if isinstance(k, str) and k.strip():
                        cache[s] = k.strip()
            log(f"  Kategorien: {len(offen)} Produkte neu zugeordnet")
            CACHE_DIR.mkdir(exist_ok=True)
            cache_datei.write_text(json.dumps(cache, ensure_ascii=False, indent=0), encoding="utf-8")
        except Exception as e:
            log(f"  Kategorien: FEHLER – {e}")

    for a in angebote:
        if not a["k"]:
            a["k"] = cache.get(schluessel(a), "")


# =============================================================================
# Ablauf
# =============================================================================

def versuche(name: str, funktion, status: dict) -> list[dict]:
    """Eine Quelle abrufen; Fehler protokollieren statt alles abzubrechen."""
    t0 = time.time()
    try:
        daten = funktion()
        status[name] = {"ok": True, "anzahl": len(daten)}
        log(f"  {name}: {len(daten)} Angebote ({time.time() - t0:.1f}s)")
        return daten
    except Exception as e:
        status[name] = {"ok": False, "fehler": str(e)[:200]}
        log(f"  {name}: FEHLER – {e}")
        return []


def main():
    parser = argparse.ArgumentParser(description="Angebote für die Prospekt-App sammeln")
    parser.add_argument("--region", choices=REGIONEN.keys(), help="Nur eine Region")
    args = parser.parse_args()

    regionen = [args.region] if args.region else list(REGIONEN)
    DATEN_DIR.mkdir(parents=True, exist_ok=True)
    stand = datetime.now(timezone.utc).astimezone().isoformat(timespec="minutes")

    # Überregionale Quellen nur einmal abrufen
    log("Überregionale Quellen:")
    global_status: dict = {}
    aldi = versuche("Aldi", aldi_alle_angebote, global_status)
    kategorien_ergaenzen(aldi)
    rossmann = versuche("Rossmann", rossmann_alle_angebote, global_status)
    kk = None  # erst bei Bedarf
    kk_status: dict = {}

    index = {"stand": stand, "regionen": []}
    for rid in regionen:
        r = REGIONEN[rid]
        log(f"\nRegion {r['name']}:")
        status = dict(global_status)
        angebote = list(aldi) + list(rossmann)

        for plz in r["plz"]:
            angebote += versuche(f"marktguru {plz}", lambda p=plz: mg_alle_angebote(p), status)

        if r["kk"]:
            if kk is None:
                kk = versuche("K+K", kk_alle_angebote, kk_status)
            status["K+K"] = kk_status["K+K"]
            angebote += kk

        angebote = doppelte_entfernen(angebote)
        maerkte = sorted({a["h"] for a in angebote})
        datei = DATEN_DIR / f"{rid}.json"
        datei.write_text(
            json.dumps(
                {"region": rid, "name": r["name"], "stand": stand, "maerkte": maerkte,
                 "quellen": status, "angebote": angebote},
                ensure_ascii=False, separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        log(f"  => {len(angebote)} Angebote, {len(maerkte)} Märkte, "
            f"{datei.stat().st_size / 1024:.0f} KB")
        index["regionen"].append({"id": rid, "name": r["name"], "maerkte": maerkte,
                                  "anzahl": len(angebote)})

    # Index für die App (bei --region die übrigen Regionen aus altem Index übernehmen)
    index_datei = DATEN_DIR / "index.json"
    if args.region and index_datei.exists():
        alt = json.loads(index_datei.read_text(encoding="utf-8"))
        neu_ids = {x["id"] for x in index["regionen"]}
        index["regionen"] += [x for x in alt.get("regionen", []) if x["id"] not in neu_ids]
    index["regionen"].sort(key=lambda x: list(REGIONEN).index(x["id"]))
    index_datei.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")

    # Diagnose-Datei: alle Händler, die marktguru pro PLZ liefert (auch aussortierte)
    (BASIS / "cache" / "marktguru_haendler.json").write_text(
        json.dumps(MG_HAENDLER_DIAGNOSE, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log("\nFertig.")


if __name__ == "__main__":
    main()
