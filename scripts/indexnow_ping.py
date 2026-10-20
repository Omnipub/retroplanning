"""Signale les URL du site a IndexNow (Bing, Yandex, Seznam, Naver...).

Usage :
    python scripts/indexnow_ping.py              # toutes les URL du sitemap
    python scripts/indexnow_ping.py URL [URL...] # seulement ces URL

A lancer apres chaque publication ou modification de contenu. Bing alimente
aussi la recherche de ChatGPT et de Copilot : une indexation rapide sur Bing
compte donc pour la visibilite dans les moteurs IA.
"""
import json
import os
import sys
import urllib.request
import xml.etree.ElementTree as ET

SITE_URL = "https://www.retroplanning.eu"
HOST = "www.retroplanning.eu"
KEY = os.environ.get("INDEXNOW_KEY", "94a2325a2306a8657aa0f9a7fc56d46d")


def sitemap_urls():
    with urllib.request.urlopen(SITE_URL + "/sitemap.xml", timeout=30) as resp:
        tree = ET.fromstring(resp.read())
    ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    return [loc.text for loc in tree.findall(".//s:loc", ns)]


def ping(urls):
    payload = json.dumps({
        "host": HOST,
        "key": KEY,
        "keyLocation": SITE_URL + "/" + KEY + ".txt",
        "urlList": urls,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.indexnow.org/indexnow",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


if __name__ == "__main__":
    urls = sys.argv[1:] or sitemap_urls()
    status = ping(urls)
    # 200 = recu, 202 = recu (cle en cours de verification)
    print(f"IndexNow : {len(urls)} URL envoyees, statut HTTP {status}")
