"""Einmalige Erkundung: Wie ist der K+K-Prospekt auf klaas-und-kock.de eingebunden?"""
import json, re, requests
from urllib.parse import urljoin
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"
erg = {}
def holen(url):
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30, allow_redirects=True)
        return r
    except Exception as e:
        erg.setdefault("fehler", {})[url] = str(e)[:200]
start = "https://www.klaas-und-kock.de/"
r = holen(start)
if r is not None:
    erg["start"] = {"status": r.status_code, "url": r.url, "len": len(r.text)}
    links = sorted(set(urljoin(r.url, h) for h in re.findall(r'(?:href|src)="([^"]+)"', r.text)))
    kand = [l for l in links if re.search(r"prospekt|kw|pdf|blaetter|flip|yumpu|issuu|angebot|publuu|3dissue|catalog", l, re.I)]
    erg["kandidaten"] = kand[:60]
    for l in kand[:15]:
        r2 = holen(l)
        if r2 is None: continue
        info = {"status": r2.status_code, "url": r2.url, "typ": r2.headers.get("Content-Type"), "len": len(r2.content)}
        if "html" in (info["typ"] or ""):
            t = r2.text
            info["titel"] = (re.search(r"<title>(.*?)</title>", t, re.S) or [None, ""])[1][:120] if re.search(r"<title>", t) else ""
            info["iframes"] = re.findall(r'<iframe[^>]+src="([^"]+)"', t)[:10]
            info["pdf"] = sorted(set(re.findall(r'["\'(]([^"\'()]+\.pdf[^"\'()]*)', t)))[:10]
            info["bilder"] = sorted(set(re.findall(r'["\'(]([^"\'()]+\.(?:jpe?g|png|webp)[^"\'()]*)', t)))[:40]
            info["kw"] = sorted(set(re.findall(r'[^"\'<>]{0,80}KW[ _]?\d{1,2}[^"\'<>]{0,80}', t)))[:10]
        erg.setdefault("seiten", {})[l] = info
open("cache/kk_erkunden.json", "w").write(json.dumps(erg, ensure_ascii=False, indent=1))
