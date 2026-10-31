"""Einmalig: kaufDA-Händlerseiten für Edeka in Altenberge und Greven finden."""
import json, re, requests
UA = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36"
erg = {}
def seite(url):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    return r
for stadt in ["Altenberge", "Greven", "Greven-Westf", "Greven-Westfalen"]:
    r = seite(f"https://www.kaufda.de/{stadt}")
    hrefs = sorted(set(re.findall(r'href="([^"]+)"', r.text)))
    pr = [h for h in hrefs if "/p-r" in h or "edeka" in h.lower()]
    erg[stadt] = {"status": r.status_code, "url": r.url, "p_links": pr[:120], "edeka_im_text": len(re.findall("edeka", r.text, re.I))}
# direkte Versuche mit bekannten Händler-IDs
for stadt in ["Altenberge", "Greven"]:
    for name in ["EDEKA", "Edeka", "E-center", "Marktkauf"]:
        for rid in ["p-r34", "p-r96", "p-r53", "p-r110", "p-r1"]:
            pass
open("cache/edeka_erkunden.json", "w").write(json.dumps(erg, ensure_ascii=False, indent=1))
