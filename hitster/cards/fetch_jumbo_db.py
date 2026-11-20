#!/usr/bin/env python3
"""Pobiera bazę kart Hitstera od Jumbo i pokazuje, co się zmieniło.

Użycie:
    python3 hitster/cards/fetch_jumbo_db.py            # pobierz i nadpisz gameset_database.json
    python3 hitster/cards/fetch_jumbo_db.py --check    # tylko porównaj, nic nie zapisuj
    python3 hitster/cards/fetch_jumbo_db.py --sku aaae0001 aaae0007   # zmiany tylko w tych edycjach
    python3 hitster/cards/fetch_jumbo_db.py --list [aaae]   # edycje z lokalnej kopii (bez sieci)

Na evencie używamy kopii z repo, nie pobieramy bazy na żywo.
"""
import argparse
import datetime
import json
import pathlib
import sys
import urllib.request

URL = "https://hitster.jumboplay.com/hitster-assets/gameset_database.json"
HERE = pathlib.Path(__file__).resolve().parent
LOCAL = HERE / "gameset_database.json"


def index(db):
    """{sku: {"name": ..., "lang": ..., "cards": {numer_karty: spotify_id}}}"""
    out = {}
    for g in db["gamesets"]:
        d = g["gameset_data"]
        out[g["sku"]] = {
            "name": d["gameset_name"].strip(),
            "lang": d["gameset_language"].strip(),
            "cards": {int(c["CardNumber"]): c["Spotify"] for c in d["cards"]},
        }
    return out


def stamp(db):
    return datetime.datetime.fromtimestamp(db["updated_on"]).strftime("%Y-%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="tylko porównaj z lokalną kopią")
    ap.add_argument("--sku", nargs="*", help="pokaż zmiany tylko dla tych edycji")
    ap.add_argument("--list", nargs="?", const="", metavar="PREFIKS",
                    help="wypisz edycje z lokalnej kopii (opcjonalnie tylko SKU o prefiksie)")
    args = ap.parse_args()

    if args.list is not None:
        db = json.loads(LOCAL.read_text(encoding="utf-8"))
        print(f"Lokalna kopia: {len(db['gamesets'])} edycji, baza z {stamp(db)}")
        for s, d in sorted(index(db).items()):
            if s.startswith(args.list):
                print(f"{s}  {len(d['cards']):4d}  {d['lang']:<12} {d['name']}")
        return 0

    req = urllib.request.Request(URL, headers={"User-Agent": "hitsteroke/1.0"})
    raw = urllib.request.urlopen(req, timeout=60).read()
    new = json.loads(raw)
    print(f"Pobrano: {len(new['gamesets'])} edycji, baza z {stamp(new)}")

    if LOCAL.exists():
        old = json.loads(LOCAL.read_text(encoding="utf-8"))
        print(f"Lokalna kopia: {len(old['gamesets'])} edycji, baza z {stamp(old)}")
        a, b = index(old), index(new)
        skus = args.sku or sorted(set(a) | set(b))
        changed = 0
        for s in skus:
            if s not in a and s in b:
                print(f"  NOWA   {s} {b[s]['lang']} | {b[s]['name']} ({len(b[s]['cards'])} kart)")
                changed += 1
            elif s in a and s not in b:
                print(f"  USUNIETA {s} {a[s]['lang']} | {a[s]['name']}")
                changed += 1
            elif s in a and s in b and a[s]["cards"] != b[s]["cards"]:
                ca, cb = a[s]["cards"], b[s]["cards"]
                diff = [n for n in sorted(set(ca) | set(cb)) if ca.get(n) != cb.get(n)]
                print(f"  ZMIANA {s} {b[s]['lang']} | {b[s]['name']}: {len(diff)} kart, np. {diff[:10]}")
                changed += 1
        print("Bez zmian." if not changed else f"Zmienionych edycji: {changed}")
        if changed and not args.check:
            print("Po aktualizacji sprawdź tabele kart (złączenie po ID Spotify).")

    if args.check:
        return 0
    LOCAL.write_bytes(raw)
    print(f"Zapisano {LOCAL.relative_to(HERE.parent.parent)} ({len(raw)} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
