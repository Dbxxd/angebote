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
from datetime import datetime, timedelta, timezone
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
# plz:    PLZs, für die marktguru abgefragt wird (Ergebnisse werden zusammengeführt)
# latlng: Ortsmitte, für die kaufDA-Abfragen
# kaufda: Märkte, die marktguru dort nicht liefert -> über ihre kaufDA-Händlerseite.
#         Hat kaufDA Einzelangebote, werden die genommen; sonst liest Claude die
#         Prospektseiten aus (einmal pro Prospekt, mit Cache).
REGIONEN = {
    "steinfurt": {
        "name": "Emsdetten / Steinfurt",
        "plz": ["48282", "48341", "48356", "48565"],
        "latlng": ("52.1758", "7.52741"),
        "kaufda": {"K+K": "/Emsdetten/K-K-Klaas-Kock/p-r586",
                   "E center": "/Steinfurt/E-center/p-r110"},  # E center Stroetmann Borghorst
    },
    "winsen": {
        "name": "Winsen (Luhe)",
        "plz": ["21423"],
        "latlng": ("53.3576", "10.2108"),
        "kaufda": {
            "famila": "/Winsen-Luhe/famila-Nordost/p-r182",
            "Norma": "/Winsen-Luhe/Norma/p-r78",
        },
    },
    "harburg": {
        "name": "Hamburg-Harburg",
        "plz": ["21073"],
        "latlng": ("53.4607", "9.9836"),
        "kaufda": {},
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

# Diese Märkte kommen überall aus eigenen Quellen; marktguru-Treffer dafür werden
# ignoriert, damit nichts doppelt auftaucht. (Pro Region kommen die "kaufda"-Märkte dazu.)
EIGENE_QUELLE = {"Aldi"}
# Diese Märkte kommen bevorzugt aus eigener Quelle; marktguru nur, wenn die eigene leer ist
RUECKFALL_MARKTGURU = {"Rossmann"}


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


def angebot(markt, marke, name, preis, alt_preis, gp, ge, packung, von, bis, kategorie, quelle,
            bild=""):
    """Einheitliches, kompaktes Format (kurze Schlüssel halten die JSON-Datei klein).
    bild: Bild-URL der Quelle, oder 'mg:<id>' für marktguru (die App baut die URL)."""
    preis, alt_preis, gp = zahl(preis), zahl(alt_preis), zahl(gp)
    if alt_preis is not None and preis is not None and alt_preis <= preis:
        alt_preis = None
    ge = ge if ge in ("kg", "l") else ""
    if gp is None or not ge:
        if preis:
            gp, ge = grundpreis_berechnen(preis, packung)
        else:
            gp, ge = None, ""
    marke = (marke or "").strip()
    if re.fullmatch(r"(?i)thisisnobrand\d*|no ?brand|ohne marke", marke):
        marke = ""  # Platzhalter von marktguru für Produkte ohne Marke
    return {
        "h": markt,
        "m": marke,
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
        **({"b": bild} if bild else {}),
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
            mg_datum(v.get("from")), mg_datum(v.get("to")),
            kat, "marktguru",
            f"mg:{o['id']}" if o.get("id") and (o.get("images") or {}).get("count") else "",
        ))
    return ergebnis


# =============================================================================
# Quelle: Aldi Nord
# =============================================================================

ALDI_SEITEN = [
    "https://www.aldi-nord.de/angebote.html",
    "https://www.aldi-nord.de/angebote-vorschau.html",
]


def mg_datum(s) -> str:
    """marktguru liefert UTC ("2026-11-01T23:00:00Z" = Montag 0 Uhr bei uns) -> lokales Datum."""
    if not s:
        return ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone().date().isoformat()
    except ValueError:
        return s[:10]


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

    def aldi_bild(p: dict) -> str:
        assets = p.get("assets") or []
        haupt = next((x for x in assets if x.get("type") == "primary"), assets[0] if assets else {})
        url = haupt.get("url") or ""
        if url.startswith("http") and "scene7" in url and "?" not in url:
            url += "?wid=240&hei=240&fit=constrain"  # kleine Vorschau statt Originalgröße
        return url if url.startswith("http") else ""

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
            "", "aldi-nord.de", aldi_bild(p),
        ))
    return ergebnis


# =============================================================================
# Quelle: kaufDA
# Einzelangebote, wenn vorhanden (Rossmann, Penny, ...); sonst Prospektbilder,
# die Claude ausliest (K+K, ggf. famila/Norma).
# =============================================================================

KAUFDA_BASIS = "https://www.kaufda.de"
ROSSMANN_SEITE = "/Emsdetten/Rossmann/p-r98"  # Rossmann-Prospekt ist bundesweit gleich
KAUFDA_PAGES_API = "https://content-viewer-be.kaufda.de/v1/brochures/{id}/pages"
EMSDETTEN = ("52.1758", "7.52741")
MIN_EINZELANGEBOTE = 5  # darunter gilt der Prospekt als "nur Bilder" -> KI
KAUFDA_DIAGNOSE: dict[str, list[str]] = {}  # Händlerseite -> gefundene Prospekte


def menge_aus_text(text: str) -> str:
    """Holt die Mengenangabe aus einem Werbetext: 'Praktisch grätenfrei. 100 g' -> '100 g'."""
    m = re.search(
        r"(?:je\s+)?\d+(?:[.,]\d+)?\s*(?:x\s*\d+(?:[.,]\d+)?\s*)?-?\s*"
        r"(?:kg|g|ml|l|cl|Stück|Stk\.?|St\.)(?:-[A-Za-zäöü]+)?\b",
        text or "", re.I,
    )
    return m.group(0).strip() if m else ""


def kaufda_datum(s: str) -> str:
    if not s:
        return ""
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z").astimezone().date().isoformat()
    except ValueError:
        return s[:10]


def kaufda_prospekte(seite: str) -> list[dict]:
    url = seite if seite.startswith("http") else KAUFDA_BASIS + seite
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise RuntimeError("kaufDA: keine Seitendaten gefunden")
    d = json.loads(m.group(1))
    broschueren = d["props"]["pageProps"]["pageInformation"]["brochures"]

    # "viewer" ist nur der oben gezeigte Prospekt; "publisher" enthält alle des Händlers
    heute = datetime.now().date().isoformat()
    gefunden: dict[str, dict] = {}
    for schluessel in ("viewer", "publisher"):
        wert = broschueren.get(schluessel) or []
        eintraege = wert.get("items", []) if isinstance(wert, dict) else wert
        for b in eintraege:
            if not isinstance(b, dict) or not b.get("contentId"):
                continue
            KI_DIAGNOSE.setdefault("broschueren_roh", {})[b["contentId"]] = {
                k: (v if isinstance(v, (str, int, float, bool)) or v is None else type(v).__name__)
                for k, v in b.items()}
            if b.get("type") == "DYNAMIC":
                continue  # Werbekarten ohne Prospektseiten (z.B. "Modern Classics")
            p = {
                "id": b["contentId"],
                "titel": b.get("title", ""),
                "von": kaufda_datum(b.get("validFrom", "")),
                "bis": kaufda_datum(b.get("validUntil", "")),
            }
            if p["bis"] and p["bis"] < heute:
                continue  # abgelaufen
            gefunden.setdefault(p["id"], p)
    KAUFDA_DIAGNOSE[url] = [f"{p['titel']} ({p['von']} bis {p['bis']})" for p in gefunden.values()]
    return list(gefunden.values())


def kaufda_seiten(prospekt_id: str, latlng: tuple[str, str] = EMSDETTEN) -> list[dict]:
    r = requests.get(
        KAUFDA_PAGES_API.format(id=prospekt_id),
        params={"partner": "kaufda_web", "lat": latlng[0], "lng": latlng[1]},
        headers={"User-Agent": UA},
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    inhalt = j.get("contents", []) if isinstance(j, dict) else []
    if not inhalt:  # für die Fehlersuche festhalten, was kaufDA stattdessen liefert
        KI_DIAGNOSE.setdefault("leere_seiten_api", {})[prospekt_id] = {
            "status": r.status_code, "schluessel": list(j)[:15] if isinstance(j, dict) else type(j).__name__,
            "anfang": r.text[:600]}
    return inhalt


# Herkunftsländer stehen bei Obst/Gemüse manchmal im Markenfeld ("Niederlande Rosenkohl")
LAENDER = {"deutschland", "niederlande", "spanien", "italien", "frankreich", "griechenland", "türkei",
           "marokko", "ägypten", "peru", "chile", "südafrika", "neuseeland", "costa rica", "kolumbien",
           "ecuador", "israel", "polen", "belgien", "österreich", "portugal", "brasilien", "argentinien",
           "mexiko", "indien", "china", "usa", "kenia", "dänemark", "irland", "norwegen"}


def kaufda_einzelangebot(markt: str, o: dict, prospekt: dict) -> dict:
    c = o.get("content", {})
    p = (c.get("products") or [{}])[0]
    preise = {d.get("type"): d.get("min") for d in (c.get("deals") or [])}
    gueltig = ((c.get("publicationProfiles") or [{}])[0].get("validity") or {})
    beschreibung = " ".join(x.get("paragraph", "") for x in (p.get("description") or [])).strip()
    kats = [k.get("name", "") for k in (p.get("categoryPaths") or [])]
    # Produktbild bevorzugen (freigestellt), sonst Ausschnitt aus dem Prospekt
    bilder = [i.get("url") for i in (p.get("images") or []) if isinstance(i, dict) and i.get("url")]
    bild = bilder[0] if bilder else (c.get("image") if isinstance(c.get("image"), str) else "")
    marke = p.get("brandName") or ""
    if marke.strip().lower() in LAENDER:
        marke = ""
    return angebot(
        markt,
        marke, p.get("name"),
        preise.get("SALES_PRICE") or None, preise.get("REGULAR_PRICE") or None,
        None, "",
        menge_aus_text(beschreibung),
        kaufda_datum(gueltig.get("startDate", "")) or prospekt["von"],
        kaufda_datum(gueltig.get("endDate", "")) or prospekt["bis"],
        kats[-1] if kats else "", "kaufDA",
        bild if (bild or "").startswith("http") else "",
    )


def kaufda_markt_angebote(markt: str, seite: str, latlng: tuple[str, str] = EMSDETTEN) -> list[dict]:
    """Alle aktuellen Angebote eines Händlers über seine kaufDA-Seite."""
    ergebnis = []
    for prospekt in kaufda_prospekte(seite):
        seiten = kaufda_seiten(prospekt["id"], latlng)
        einzeln = [o for s in seiten for o in s.get("offers", [])]
        if len(einzeln) >= MIN_EINZELANGEBOTE:
            ergebnis += [kaufda_einzelangebot(markt, o, prospekt) for o in einzeln]
        else:
            for a in ki_prospekt_auslesen(markt, prospekt, seiten):
                ergebnis.append(angebot(
                    markt, a.get("marke"), a.get("name"),
                    a.get("preis"), a.get("alt_preis"),
                    a.get("grundpreis"), a.get("gp_einheit"),
                    a.get("packung"),
                    prospekt["von"], prospekt["bis"],
                    a.get("kategorie"), "kaufDA+KI", ki_bildausschnitt(a),
                ))
            ergebnis = ki_doppelte_entfernen(ergebnis)
    return ergebnis


# --- K+K direkt von klaas-und-kock.de (Blätterkatalog von COMINTO) ---
KK_KATALOG = "https://www.klaas-und-kock.de/kataloge/blaetterkatalog/"


def kk_webseite_angebote() -> list[dict]:
    """K+K-Wochenprospekt von der eigenen Webseite: Seitenbilder per KI auslesen."""
    r = requests.get(KK_KATALOG, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    m = re.search(r'catalog:\s*"([^":]+)::catalog"', r.text)
    if not m:
        raise RuntimeError("K+K: Katalogname nicht gefunden")
    name = m.group(1)
    titel = (re.search(r"<title>(.*?)</title>", r.text, re.S) or [None, name])[1].strip()
    basis = f"{KK_KATALOG}catalogs/{name}/"
    x = requests.get(basis + "xml/catalog.xml", headers={"User-Agent": UA}, timeout=30)
    x.raise_for_status()
    seiten_zahl = int(re.search(r'nofpages="(\d+)"', x.text).group(1))

    # Gültigkeit aus der Kalenderwoche im Titel ("K+K_KW 44"): Montag bis Samstag
    kw = re.search(r"KW\s*_?(\d{1,2})", titel) or re.search(r"kw(\d{1,2})", name)
    heute = datetime.now().date()
    von = bis = ""
    if kw:
        jahr = heute.year
        if int(kw.group(1)) < 10 and heute.month == 12:
            jahr += 1
        montag = datetime.fromisocalendar(jahr, int(kw.group(1)), 1).date()
        von, bis = montag.isoformat(), (montag + timedelta(days=5)).isoformat()
        if bis < heute.isoformat():
            raise RuntimeError(f"K+K: Prospekt {titel} ist abgelaufen")
    prospekt = {"id": f"kkweb-{name}-{von}", "titel": titel, "von": von, "bis": bis}
    seiten = [{"images": [{"size": "1600x1600", "url": f"{basis}large/bk_{i}.jpg"}]}
              for i in range(1, seiten_zahl + 1)]
    return ki_prospekt_angebote("K+K", prospekt, seiten)


def ki_prospekt_angebote(markt: str, prospekt: dict, seiten: list[dict]) -> list[dict]:
    ergebnis = []
    for a in ki_prospekt_auslesen(markt, prospekt, seiten):
        ergebnis.append(angebot(
            markt, a.get("marke"), a.get("name"),
            a.get("preis"), a.get("alt_preis"),
            a.get("grundpreis"), a.get("gp_einheit"),
            a.get("packung"),
            prospekt["von"], prospekt["bis"],
            a.get("kategorie"), "K+K-Webseite+KI" if prospekt["id"].startswith("kkweb") else "kaufDA+KI",
            ki_bildausschnitt(a),
        ))
    return ki_doppelte_entfernen(ergebnis)


def kk_angebote(seite: str, latlng) -> list[dict]:
    """Erst die K+K-Webseite, bei Problemen kaufDA."""
    try:
        web = kk_webseite_angebote()
        if web:
            return web
        log("  K+K-Webseite: keine Angebote, versuche kaufDA")
    except Exception as e:
        log(f"  K+K-Webseite fehlgeschlagen ({e}), versuche kaufDA")
    return kaufda_markt_angebote("K+K", seite, latlng)


def rossmann_alle_angebote() -> list[dict]:
    return kaufda_markt_angebote("Rossmann", ROSSMANN_SEITE)


# --- Prospektbilder per Claude auslesen (K+K und andere ohne Einzelangebote) ---

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
  "kategorie": "ein allgemeines deutsches Wort für die Produktart, z.B. Butter, Bier, Kaffee, Käse, Obst, Fleisch",
  "bereich": [links, oben, rechts, unten]
}

"bereich" ist der Rahmen um das PRODUKTFOTO dieses Angebots auf der Seite (ohne Preisschild
und Text), in Prozent der Seitenbreite bzw. -höhe als ganze Zahlen von 0 bis 100.
Beispiel: ein Foto im linken oberen Viertel wäre etwa [3, 4, 45, 30]. Gibt es kein Foto: null.

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


KI_DIAGNOSE: dict = {}  # landet in cache/ki_diagnose.json
MAX_KI_SEITEN = 40  # Schutz vor sehr dicken Prospekten (Kosten)
KI_CACHE_VERSION = 2  # 2 = mit Bildbereich je Angebot; ältere Caches werden neu ausgelesen


def ki_prospekt_auslesen(markt: str, prospekt: dict, seiten: list[dict]) -> list[dict]:
    """Liest einen Prospekt per Claude aus. Ergebnis wird pro Prospekt gecacht."""
    cache = CACHE_DIR / f"ki_{prospekt['id']}.json"
    if cache.exists():
        daten = json.loads(cache.read_text(encoding="utf-8"))
        if daten.get("version", 1) >= KI_CACHE_VERSION:
            return daten["angebote"]
    (CACHE_DIR / f"kk_{prospekt['id']}.json").unlink(missing_ok=True)  # Cache der Vorversion

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(f"ANTHROPIC_API_KEY fehlt – {markt} übersprungen")
    import anthropic

    client = anthropic.Anthropic()
    bilder = []
    for seite in seiten:
        b = {i.get("size"): i.get("url") for i in seite.get("images", [])}
        url = b.get("1600x1600") or (list(b.values())[-1] if b else None)
        if url:
            bilder.append(url)
    if len(bilder) > MAX_KI_SEITEN:
        log(f"  {markt}: Prospekt hat {len(bilder)} Seiten, lese nur die ersten {MAX_KI_SEITEN}")
        bilder = bilder[:MAX_KI_SEITEN]

    log(f"  {markt}: lese '{prospekt['titel']}' ({len(bilder)} Seiten) mit {KK_MODELL} aus ...")
    diag = KI_DIAGNOSE.setdefault(f"{markt} {prospekt['id']}", {
        "titel": prospekt["titel"], "seiten_api": len(seiten), "bilder": len(bilder),
        "bildgroessen": sorted({i.get("size") for s in seiten for i in s.get("images", [])} - {None}),
        "seitenschluessel": sorted({k for s in seiten for k in s})[:20], "je_seite": []})
    alle, ti, to = [], 0, 0
    for i, url in enumerate(bilder, 1):
        try:
            daten, a, b = kk_seite_auslesen(client, url)
        except Exception as e:  # eine kaputte Seite soll nicht den ganzen Prospekt kosten
            diag["je_seite"].append(f"Fehler: {type(e).__name__}: {e}"[:200])
            continue
        diag["je_seite"].append(len(daten))
        log(f"    Seite {i}: {len(daten)} Angebote")
        for d in daten:
            d["seiten_bild"] = url
        alle.extend(daten)
        ti, to = ti + a, to + b
    log(f"  {markt}: {len(alle)} Angebote, {ti} Tokens rein / {to} raus")
    if not alle:
        # Nichts gefunden (Seiten noch nicht online o.ä.): nicht merken, beim nächsten Lauf neu versuchen
        log(f"  {markt}: keine Angebote erkannt – wird beim nächsten Lauf erneut versucht")
        return alle

    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_text(
        json.dumps({"version": KI_CACHE_VERSION, "markt": markt, "prospekt": prospekt,
                    "modell": KK_MODELL, "angebote": alle},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return alle


def ki_bildausschnitt(a: dict) -> str:
    """'ks:<Seitenbild-URL>#l,o,r,u' – die App zeigt daraus den Ausschnitt der Prospektseite."""
    url, b = a.get("seiten_bild") or "", a.get("bereich")
    if not url.startswith("https://") or not isinstance(b, list) or len(b) != 4:
        return ""
    try:
        l, o, r, u = (max(0.0, min(100.0, float(x))) for x in b)
    except (TypeError, ValueError):
        return ""
    if r - l < 3 or u - o < 3:  # zu klein / unsinnig
        return ""
    return f"ks:{url}#{l:g},{o:g},{r:g},{u:g}"


def ki_doppelte_entfernen(angebote: list[dict]) -> list[dict]:
    """Titelseite und Innenteil zeigen oft dasselbe Angebot (leicht anders formuliert)."""
    gesehen, eindeutig = set(), []
    for a in angebote:
        if a["q"] != "kaufDA+KI":
            eindeutig.append(a)
            continue
        w = re.findall(r"[a-zäöüß]+", a["n"].lower())
        s = (a["h"], a["p"], w[0] if w else a["n"])
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
# Feste Gruppen zum Stöbern in der App
# =============================================================================

# Gruppe -> Untergruppen. Reihenfolge = Reihenfolge in der App (wie im Laden: frisch zuerst).
UNTERGRUPPEN = {
    "Obst & Gemüse": ["Obst", "Gemüse", "Kartoffeln & Zwiebeln", "Salat & Kräuter"],
    "Fleisch & Wurst": ["Rind", "Schwein", "Geflügel", "Hackfleisch", "Grillgut & Bratwurst",
                        "Lamm & Wild", "Aufschnitt & Wurst", "Schinken & Salami", "Fleischersatz"],
    "Fisch": ["Frischer Fisch", "Räucherfisch", "Fischkonserven & Feinkost", "Garnelen & Meeresfrüchte"],
    "Milch, Käse & Eier": ["Milch & Milchgetränke", "Butter & Margarine", "Scheibenkäse",
                           "Käse am Stück & gerieben", "Weichkäse", "Frischkäse & Streichkäse",
                           "Back-, Ofen- & Grillkäse", "Naturjoghurt & Quark", "Frucht- & Sahnejoghurt",
                           "Sahne & Desserts", "Eier"],
    "Brot & Backwaren": ["Brot & Brötchen", "Toast & Sandwich", "Zwieback & Knäckebrot"],
    "Tiefkühl": ["Tiefkühl-Pizza & Snacks", "Tiefkühl-Gemüse & -Obst", "Pommes & Kartoffelprodukte",
                 "Eis", "Tiefkühl-Fisch & -Fleisch", "Tiefkühl-Fertiggerichte"],
    "Fertiggerichte & Konserven": ["Fertiggerichte", "Suppen & Eintöpfe", "Gemüse- & Obstkonserven",
                                   "Feinkost & Salate"],
    "Vorrat & Backen": ["Nudeln & Reis", "Mehl, Zucker & Backzutaten", "Öl & Essig",
                        "Saucen, Ketchup & Senf", "Gewürze & Brühe"],
    "Frühstück & Aufstriche": ["Müsli & Cerealien", "Marmelade & Honig", "Nuss-Nougat- & Brotaufstriche"],
    "Süßes & Snacks": ["Schokolade & Riegel", "Fruchtgummi & Bonbons", "Kekse & Gebäck",
                       "Kuchen & Feingebäck", "Chips & Knabbereien", "Nüsse & Trockenobst"],
    "Kaffee & Tee": ["Kaffeebohnen", "Gemahlener Kaffee", "Pads & Kapseln", "Instantkaffee", "Tee & Kakao"],
    "Getränke": ["Wasser", "Saft & Schorle", "Limonade & Cola", "Energy- & Sportgetränke",
                 "Kaffee- & Teegetränke"],
    "Bier": ["Pils & Helles", "Weizenbier", "Alkoholfrei & Radler", "Spezialitäten & Mix"],
    "Wein & Sekt": ["Rotwein", "Weißwein", "Roséwein", "Sekt & Prosecco", "Glühwein"],
    "Spirituosen": ["Likör", "Whisky", "Rum", "Wodka & Gin", "Weitere Spirituosen"],
    "Drogerie & Pflege": ["Körper- & Gesichtspflege", "Haarpflege", "Zahnpflege", "Kosmetik & Parfüm",
                          "Gesundheit & Nahrungsergänzung", "Damenhygiene & Rasur"],
    "Baby & Kind": ["Babynahrung", "Windeln & Babypflege", "Spielzeug", "Kinderbedarf"],
    "Haushalt & Reinigung": ["Waschmittel", "Reinigung & Spülen", "Papier & Folien",
                             "Küche & Kochen", "Haushaltsgeräte"],
    "Tierbedarf": ["Katze", "Hund", "Weitere Tiere"],
    "Kleidung & Schuhe": ["Damen", "Herren", "Kinder", "Schuhe", "Wäsche & Socken"],
    "Haus, Garten & Deko": ["Deko & Saisonartikel", "Heimtextilien", "Garten & Pflanzen", "Möbel & Aufbewahrung",
                            "Kerzen & Licht"],
    "Elektronik & Technik": ["Unterhaltungselektronik", "Werkzeug & Heimwerken", "Auto & Fahrrad",
                             "Weitere Technik"],
    "Sonstiges": ["Freizeit & Sport", "Bücher & Schreibwaren", "Rabatte & Coupons", "Weiteres"],
}
GRUPPEN = list(UNTERGRUPPEN)

# Codes wie "11.3" halten die Antwort der KI kurz
OPTIONEN = {
    f"{gi}.{ui}": (g, u)
    for gi, (g, us) in enumerate(UNTERGRUPPEN.items(), 1)
    for ui, u in enumerate(us, 1)
}
CODE_VON = {v: c for c, v in OPTIONEN.items()}


def _als_name(gu: tuple[str, str]) -> str:
    """Im Cache stehen Namen ('Gruppe|Unter'), keine Codes – so überlebt der Cache
    Änderungen an der Liste. Nicht mehr existierende Einträge werden neu gefragt."""
    return f"{gu[0]}|{gu[1]}"

GRUPPEN_PROMPT = """Ordne jedes Produkt aus einem Supermarkt-Prospekt genau EINER Unterkategorie zu.
Antworte mit deren Code.

{optionen}

Wichtig:
- Fertige Getränke gehören zu "Getränke", auch wenn Kaffee oder Tee drin ist
  ("Starbucks Chilled Coffee", "Iced Coffee", "Eistee" -> Kaffee- & Teegetränke).
- "Kaffee & Tee" nur für Bohnen, Pulver, Pads, Kapseln, Instant, Teebeutel, Kakaopulver.
- Butter und Margarine -> Butter & Margarine. Buttermilch, Kakaodrink -> Milch & Milchgetränke.
- Kaffeemaschinen, Wasserkocher usw. -> Haushaltsgeräte.
- Zwieback, Knäckebrot, Grissini -> Zwieback & Knäckebrot (bei Brot & Backwaren).
- Käse: Scheiben -> Scheibenkäse; Stück, Würfel, gerieben -> Käse am Stück & gerieben;
  Camembert, Brie, Weichkäse -> Weichkäse; Frischkäse, Frischkäsezubereitung, körniger
  Frischkäse, Miree, Almette, Bresso, Streichkäse -> Frischkäse & Streichkäse;
  Back-Camembert, Backkäse, Ofenkäse, Grillkäse, Halloumi -> Back-, Ofen- & Grillkäse.
- Kuchen, Stollen, Torten, Muffins, Croissants, Berliner -> Kuchen & Feingebäck (bei Süßes & Snacks).
- Obst und Gemüse in Dose oder Glas (Litschis, Mais, Rotkohl im Glas, Apfelmus)
  -> Gemüse- & Obstkonserven (bei Fertiggerichte & Konserven).
- Fertige Salate aus dem Kühlregal (Kartoffel-, Nudel-, Fleisch-, Kraut-, Gurken-, Beilagensalat),
  Dips, Tzatziki, Hummus, Antipasti -> Feinkost & Salate (bei Fertiggerichte & Konserven).
  "Salat & Kräuter" (Obst & Gemüse) nur für frischen Salat wie Eisberg, Rucola, Feldsalat.
- Herzhafte Brotaufstriche (Brunch, Popp Brotaufstrich) -> Nuss-Nougat- & Brotaufstriche.
- Adventskalender nach Inhalt: Schokolade -> Schokolade & Riegel, Chips -> Chips & Knabbereien,
  Spiele/Spielzeug -> Spielzeug.
- Lesebrillen, Rollatoren, Rollstühle, Blutdruckmessgeräte -> Gesundheit & Nahrungsergänzung.
- Getränke immer nach Inhalt, auch bei seltsamen Namen ("Near Water", "Infused Water" -> Wasser).
- Grillanzünder, Anzündholz, Holzbriketts, Grillkohle -> Garten & Pflanzen (kein Fleisch!).
- Joghurt: Naturjoghurt, griechischer Joghurt, Skyr natur, Speisequark, Magerquark,
  Kräuterquark -> Naturjoghurt & Quark; mit Frucht, Geschmack oder Sahne (Fruchtzwerge,
  Almighurt, Sahnejoghurt, Fruchtquark, Joghurt mit der Ecke) -> Frucht- & Sahnejoghurt;
  Trinkjoghurt, Actimel, Skyr-Drink -> Milch & Milchgetränke.
- Frisches Lamm, Wild, Hirsch, Reh, Wildschwein, Kaninchen -> Lamm & Wild (tiefgekühlt -> Tiefkühl).
  Nuggets, Schnitzel usw. nach Tierart: "Schweine-Nuggets" -> Schwein, "Chicken Nuggets" -> Geflügel.
- Prozent-Aktionen ohne Produkt ("20 % Rabatt auf ...") -> Rabatte & Coupons.

Antworte NUR mit einer Zeile pro Produkt im Format "Nummer: Code", z.B.
0: 11.3
1: 12.5
Keine weiteren Erklärungen.

Produkte:
"""


KAT_MODELL = os.environ.get("KATEGORIE_MODELL", "claude-sonnet-5-5")  # genauer als Haiku
KAT_PAKET = 50  # Produkte pro Anfrage; bei großen Paketen bricht das Modell gern ab


def _sicher(funktion, *args):
    """Fehler eines Pakets protokollieren statt den ganzen Lauf abzubrechen."""
    try:
        return funktion(*args)
    except Exception as e:
        log(f"  Untergruppen: FEHLER – {e}")
        return {}


def gruppen_zuordnen(angebote: list[dict]) -> None:
    """Setzt a['g'] (Gruppe) und a['u'] (Untergruppe) für jedes Angebot.
    Entschieden wird pro Produkt (Marke + Name + Kategorie); Ergebnisse werden gecacht."""
    cache_datei = CACHE_DIR / "untergruppen.json"
    cache = json.loads(cache_datei.read_text(encoding="utf-8")) if cache_datei.exists() else {}
    # Nur Einträge behalten, deren Gruppe/Unterkategorie es (noch) gibt
    gueltig = {_als_name(v) for v in OPTIONEN.values()}
    cache = {k: v for k, v in cache.items() if v in gueltig}

    def schluessel(a):
        return f"{a['m']} {a['n']} [{a['k']}]".strip().lower()

    offen = sorted({schluessel(a) for a in angebote} - set(cache))
    if offen and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
        optionen = "\n".join(f"{c} {g} > {u}" for c, (g, u) in OPTIONEN.items())
        prompt = GRUPPEN_PROMPT.format(optionen=optionen)
        def frage(teil: list[str]) -> dict[str, str]:
            """Ein Paket einordnen; liefert {Schlüssel: Code} nur für gültige Antworten."""
            liste = "\n".join(f"{i}: {s}" for i, s in enumerate(teil))
            hinweis = f"\nEs sind {len(teil)} Produkte (0 bis {len(teil) - 1}). Beantworte ALLE.\n"
            antwort = client.messages.create(
                model=KAT_MODELL, max_tokens=2000,
                messages=[{"role": "user", "content": prompt + liste + hinweis}],
            )
            text = "".join(b.text for b in antwort.content if getattr(b, "type", "") == "text")
            # Robust lesen: "0: 11.3", "0 = 11.3", {"0": "11.3"} oder {"0": 11.3}
            zuordnung = dict(re.findall(r'"?(\d+)"?\s*[:=]\s*"?(\d+\.\d+)', text))
            return {s: _als_name(OPTIONEN[zuordnung[str(i)]]) for i, s in enumerate(teil)
                    if zuordnung.get(str(i)) in OPTIONEN}

        from concurrent.futures import ThreadPoolExecutor

        neu = 0
        for runde in (1, 2):  # Runde 2: was in Runde 1 unbeantwortet blieb
            rest = [s for s in offen if s not in cache]
            if not rest:
                break
            pakete = [rest[i:i + KAT_PAKET] for i in range(0, len(rest), KAT_PAKET)]
            with ThreadPoolExecutor(max_workers=4) as pool:
                for nr, ergebnis in enumerate(pool.map(lambda t: _sicher(frage, t), pakete), 1):
                    cache.update(ergebnis)
                    neu += len(ergebnis)
                    if nr % 20 == 0 or nr == len(pakete):
                        # zwischendurch speichern, damit ein Abbruch nichts verliert
                        CACHE_DIR.mkdir(exist_ok=True)
                        cache_datei.write_text(json.dumps(cache, ensure_ascii=False, indent=0),
                                               encoding="utf-8")
            log(f"  Untergruppen Runde {runde}: {len(rest)} offen, jetzt "
                f"{sum(1 for s in rest if s in cache)} zugeordnet")
        log(f"  Untergruppen: {neu} Produkte neu zugeordnet, "
            f"{sum(1 for s in offen if s not in cache)} weiterhin offen")

    for a in angebote:
        wert = cache.get(schluessel(a), "Sonstiges|Weiteres")
        a["g"], a["u"] = wert.split("|", 1)
        # Obst und Gemüse in Dose oder Glas sind Konserven, auch wenn der Prospekt "Obst" sagt
        if a["g"] == "Obst & Gemüse" and KONSERVE.search(f'{a.get("pk") or ""} {a.get("n") or ""}'):
            a["g"], a["u"] = "Fertiggerichte & Konserven", "Gemüse- & Obstkonserven"


KONSERVE = re.compile(r"\b(dose|glas|konserve)\b|-dose\b|-glas\b", re.I)


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


# =============================================================================
# Preisverlauf: jedes Angebot wird einmal pro Markt und Aktionsbeginn gemerkt
# =============================================================================
VERLAUF_TAGE = 400


def verlauf_schluessel(a: dict) -> str:
    """Dasselbe Produkt in jedem Markt: Marke + Name, klein, ohne doppelte Leerzeichen."""
    return " ".join(f"{a.get('m') or ''} {a.get('n') or ''}".lower().split())


def _vergleich(e_p, e_gp, e_ge, a):
    """Preis eines früheren Eintrags, vergleichbar mit Angebot a (Grundpreis, wenn möglich)."""
    if a.get("gp") is not None and a.get("ge") and e_gp is not None and e_ge == a["ge"]:
        return e_gp, a["gp"], a["ge"]
    return e_p, a.get("p"), None


def verlauf_aktualisieren(rid: str, angebote: list[dict], heute: str) -> None:
    datei = DATEN_DIR / f"verlauf-{rid}.json"
    try:
        verlauf = json.loads(datei.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        verlauf = {"seit": heute, "p": {}}
    produkte = verlauf["p"]

    # 1. Neue Angebote eintragen: [datum, markt, preis, grundpreis, einheit]
    for a in angebote:
        if a.get("p") is None:
            continue
        d = a.get("von") or heute
        liste = produkte.setdefault(verlauf_schluessel(a), [])
        if not any(e[0] == d and e[1] == a["h"] and e[2] == a["p"] for e in liste):
            liste.append([d, a["h"], a["p"], a.get("gp"), a.get("ge")])

    # 2. Für jedes aktuelle Angebot: wie steht der Preis im Vergleich zu früher?
    # Gezählt wird erst ab Beginn der Aufzeichnung, ältere Aktionsdaten sagen nichts über die Zeit davor
    seit = verlauf["seit"]
    laufend = {(verlauf_schluessel(a), a["h"], a["p"], a.get("von") or heute) for a in angebote if a.get("p") is not None}
    for a in angebote:
        if a.get("p") is None:
            continue
        d = a.get("von") or heute
        frueher = [e for e in produkte.get(verlauf_schluessel(a), []) if e[0] < d]
        if not frueher:
            continue
        werte = [(e, *_vergleich(e[2], e[3], e[4], a)) for e in frueher]
        werte = [(e, w, jetzt, ge) for e, w, jetzt, ge in werte if w is not None and jetzt is not None]
        if not werte:
            continue
        tage = lambda x: (datetime.fromisoformat(d) - datetime.fromisoformat(max(x, seit))).days
        guenstiger = [(e, w, ge) for e, w, jetzt, ge in werte if w < jetzt - 0.005]
        schluessel = verlauf_schluessel(a)
        jetzt_woanders = [(e, w, ge) for e, w, ge in guenstiger if (schluessel, e[1], e[2], e[0]) in laufend]
        if jetzt_woanders:
            e, w, ge = min(jetzt_woanders, key=lambda x: x[1])
            a["vl"] = ["woanders", e[1], round(w, 2), ge]
        elif not guenstiger:
            wochen = tage(min(e[0] for e, *_ in werte)) // 7
            if wochen >= 2:
                a["vl"] = ["tief", wochen]
        else:
            e, w, ge = max(guenstiger, key=lambda x: x[0][0])
            wochen = tage(e[0]) // 7
            if wochen >= 4:
                a["vl"] = ["tief", wochen]
            elif tage(e[0]) <= 60:
                a["vl"] = ["frueher", e[0], e[1], round(w, 2), ge]

    # 3. Alte Einträge entfernen
    grenze = (datetime.fromisoformat(heute) - timedelta(days=VERLAUF_TAGE)).date().isoformat()
    for k in list(produkte):
        produkte[k] = sorted(e for e in produkte[k] if e[0] >= grenze)
        if not produkte[k]:
            del produkte[k]
    datei.write_text(json.dumps(verlauf, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"  Preisverlauf: {len(produkte)} Produkte seit {verlauf['seit']}, "
        f"{datei.stat().st_size / 1024:.0f} KB")


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

    index = {"stand": stand, "regionen": []}
    for rid in regionen:
        r = REGIONEN[rid]
        log(f"\nRegion {r['name']}:")
        status = dict(global_status)
        angebote = list(aldi) + list(rossmann)
        eigene = set(r.get("kaufda", {}))  # diese Märkte nicht zusätzlich von marktguru

        for plz in r["plz"]:
            mg = versuche(f"marktguru {plz}", lambda p=plz: mg_alle_angebote(p), status)
            # Rossmann: kaufDA hat am Wochenwechsel oft keinen Prospekt -> dann marktguru
            ohne = eigene | ({m for m in RUECKFALL_MARKTGURU if any(a["h"] == m for a in rossmann)})
            angebote += [a for a in mg if a["h"] not in ohne]

        for markt, seite in r.get("kaufda", {}).items():
            holen = kk_angebote if markt == "K+K" else (lambda s, ll, m=markt: kaufda_markt_angebote(m, s, ll))
            angebote += versuche(markt, lambda h=holen, s=seite: h(s, r["latlng"]), status)

        angebote = doppelte_entfernen(angebote)
        gruppen_zuordnen(angebote)  # Cache wird über alle Regionen geteilt
        verlauf_aktualisieren(rid, angebote, stand[:10])
        maerkte = sorted({a["h"] for a in angebote}, key=str.lower)
        datei = DATEN_DIR / f"{rid}.json"
        datei.write_text(
            json.dumps(
                {"region": rid, "name": r["name"], "stand": stand, "maerkte": maerkte,
                 "gruppen": UNTERGRUPPEN,
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

    (BASIS / "cache" / "ki_diagnose.json").write_text(
        json.dumps(KI_DIAGNOSE, ensure_ascii=False, indent=1), encoding="utf-8")
    # Diagnose-Datei: alle Händler, die marktguru pro PLZ liefert (auch aussortierte)
    (BASIS / "cache" / "kaufda_prospekte.json").write_text(
        json.dumps(KAUFDA_DIAGNOSE, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    (BASIS / "cache" / "marktguru_haendler.json").write_text(
        json.dumps(MG_HAENDLER_DIAGNOSE, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    log("\nFertig.")


if __name__ == "__main__":
    main()
