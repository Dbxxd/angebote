"""Einmalige Erkundung: Wie ist der K+K-Blätterkatalog aufgebaut?"""
import json, re, requests
from urllib.parse import urljoin
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"
B = "https://www.klaas-und-kock.de/kataloge/blaetterkatalog/"
erg = {}
r = requests.get(B, headers={"User-Agent": UA}, timeout=30)
erg["html"] = r.text[:4000]
pfade = re.findall(r'(?:href|src)="([^"]+)"', r.text)
kand = ["xml/catalog.xml", "catalog.xml", "pdf/complete.pdf", "files/assets/common/search/searchtext.json",
        "blaetterkatalog.json", "data/catalog.json", "catalog.json", "xml/content.xml", "pages.json"] + pfade
for p in kand:
    u = urljoin(B, p)
    try:
        r2 = requests.get(u, headers={"User-Agent": UA}, timeout=30)
        info = {"status": r2.status_code, "typ": r2.headers.get("Content-Type"), "len": len(r2.content)}
        if r2.ok and any(x in (info["typ"] or "") for x in ("xml", "json", "javascript", "text")):
            info["anfang"] = r2.text[:2500]
        erg[u] = info
    except Exception as e:
        erg[u] = str(e)[:200]
open("cache/kk_erkunden.json", "w").write(json.dumps(erg, ensure_ascii=False, indent=1))
