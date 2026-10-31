"""Einmalig: kaufDA-Händlerseiten für Edeka in Altenberge und Greven finden."""
import json, re, requests
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"
erg = {}
for stadt in ["Altenberge", "Greven"]:
    r = requests.get(f"https://www.kaufda.de/{stadt}", headers={"User-Agent": UA}, timeout=30)
    links = sorted(set(re.findall(r'href="(/' + stadt + r'/[^"]+/p-r\d+)"', r.text)))
    erg[stadt] = {"status": r.status_code, "links": links}
    for l in links:
        if re.search(r"edeka|e-center|marktkauf", l, re.I):
            r2 = requests.get("https://www.kaufda.de" + l, headers={"User-Agent": UA}, timeout=30)
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r2.text, re.S)
            info = {"status": r2.status_code}
            if m:
                d = json.loads(m.group(1))
                pi = d["props"]["pageProps"].get("pageInformation", {})
                br = pi.get("brochures", {})
                out = []
                for k in ("viewer", "publisher"):
                    w = br.get(k) or []
                    for b in (w.get("items", []) if isinstance(w, dict) else w):
                        if isinstance(b, dict):
                            out.append({k2: b.get(k2) for k2 in ("title", "type", "contentId", "validFrom", "validUntil", "pageCount")}
                                       | {"publisher": (b.get("publisher") or {}).get("name") if isinstance(b.get("publisher"), dict) else None})
                info["prospekte"] = out
                info["laden"] = json.dumps(pi.get("stores") or pi.get("store") or "", ensure_ascii=False)[:1500]
            erg[stadt + " " + l] = info
open("cache/edeka_erkunden.json", "w").write(json.dumps(erg, ensure_ascii=False, indent=1))
