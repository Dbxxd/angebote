"""Einmalige Erkundung: Datenpfade des K+K-Blätterkatalogs (COMINTO)."""
import json, re, requests
from urllib.parse import urljoin
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"
B = "https://www.klaas-und-kock.de/kataloge/blaetterkatalog/"
S = B + "blaetterkatalog/script/"
erg = {"treffer": {}, "proben": {}}
def get(u):
    try: return requests.get(u, headers={"User-Agent": UA}, timeout=30)
    except Exception as e: return None
start = get(S + "bk_start.js").text
namen = sorted(set(re.findall(r'ds\("([^"]+)"\)', start)))
erg["skripte"] = namen
alles = start
for n in namen:
    r = get(S + n + ".js")
    if r is not None and r.ok: alles += "\n" + r.text
muster = r'["\']([^"\'\s]{0,80}(?:catalogs?/|\.xml|\.json|\.jpg|\.webp|\.png|\.pdf|bk_|/pages?/)[^"\'\s]{0,80})["\']'
erg["pfade"] = sorted(set(re.findall(muster, alles)))[:300]
for wort in ["catalogs", "catalog.xml", "complete.pdf", "page", "zoom", "thumb", "xml/"]:
    erg["treffer"][wort] = [alles[max(0, m.start()-150):m.start()+150] for m in re.finditer(re.escape(wort), alles)][:6]
K = "kundk_kw44"
proben = [f"blaetterkatalog/catalogs/{K}/xml/catalog.xml", f"blaetterkatalog/catalogs/{K}/pdf/complete.pdf",
          f"catalogs/{K}/xml/catalog.xml", f"catalogs/{K}/pdf/complete.pdf", f"{K}/xml/catalog.xml",
          f"blaetterkatalog/catalogs/{K}/img/bk_1/1.jpg", f"blaetterkatalog/catalogs/{K}/img/pages/1.jpg",
          f"blaetterkatalog/catalogs/{K}/catalog.json", f"blaetterkatalog/catalogs/{K}/xml/settings.xml",
          f"blaetterkatalog/xml/catalog.xml", f"blaetterkatalog/catalogs/{K}/pdf/bk_1.pdf"]
for p in proben:
    r = get(urljoin(B, p))
    erg["proben"][p] = None if r is None else {"status": r.status_code, "typ": r.headers.get("Content-Type"), "len": len(r.content),
                                                "anfang": r.text[:1500] if r.ok and "xml" in (r.headers.get("Content-Type") or "") else ""}
open("cache/kk_erkunden.json", "w").write(json.dumps(erg, ensure_ascii=False, indent=1))
