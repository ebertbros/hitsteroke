#!/usr/bin/env python3
"""Buduje mapę skanera: klucz karty (sku/numer) -> folder piosenki dla komendy PLAY.

Użycie:
  python3 build_scanner_map.py aaae0001 aaae0005      # wybrane edycje
  python3 build_scanner_map.py --all                  # wszystkie editions/*.csv
  python3 build_scanner_map.py --all --tylko-pobrane  # tylko karty z folderem na dysku
  python3 build_scanner_map.py aaae0005 -o /inna/sciezka/scanner_map.json

Wejście: tabele edycji editions/<sku>.csv (z build_edition.py). Do mapy trafiają tylko
wiersze ze statusem USDB "zielony" albo "inna wersja" i z niepustą kolumną folder.
Edycję można też podać jako ścieżkę do pliku .csv, a katalog tabel zmienić opcją --katalog.

Wynik (domyślnie scanner_map.json obok tego skryptu, czyta go skaner.py):
  {"generated": "<ISO>", "editions": ["aaae0001", ...],
   "cards": {"aaae0001/00001": "hitster/Jim Croce - Bad, Bad Leroy Brown", ...}}

Domyślnie mapa zawiera wszystkie karty do pobrania, a skrypt tylko wypisuje, ilu folderów
brakuje na dysku (albo że dysk z piosenkami nie jest podłączony). Karta bez folderu na dysku
i tak kończy się w grze komunikatem „Try another card” (NOT_FOUND), więc można zbudować mapę
wcześniej i dograć piosenki później. --tylko-pobrane pomija karty bez folderu na dysku.
Tylko biblioteka standardowa Pythona.
"""

import argparse
import collections
import csv
import datetime
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hitster_cards as hc  # noqa: E402


def wczytaj_edycje(arg, katalog):
    """'aaae0005' albo ścieżka do .csv -> (sku, wiersze, ścieżka)."""
    if arg.lower().endswith(".csv") or os.sep in arg:
        path = os.path.abspath(arg)
        sku = os.path.splitext(os.path.basename(path))[0].lower()
    else:
        sku = arg.lower()
        path = os.path.join(katalog, sku + ".csv")
    if not os.path.isfile(path):
        raise SystemExit(f"Brak tabeli edycji: {path} (najpierw build_edition.py {sku})")
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return sku, rows, path


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("edycje", nargs="*", help="SKU edycji (np. aaae0005) albo ścieżki do plików .csv")
    p.add_argument("--all", action="store_true", help="wszystkie pliki *.csv z katalogu tabel edycji")
    p.add_argument("--katalog", default=hc.EDITIONS_DIR,
                   help=f"katalog tabel edycji (domyślnie {hc.EDITIONS_DIR})")
    p.add_argument("--katalog-piosenek", default=None,
                   help="katalog .../songs/hitster do sprawdzenia folderów "
                        "(domyślnie z config.ini gry albo dysk zewnętrzny)")
    p.add_argument("--tylko-pobrane", action="store_true",
                   help="pomiń karty, których folderu nie ma na dysku")
    p.add_argument("-o", "--output", default=hc.SCANNER_MAP,
                   help=f"plik wynikowy (domyślnie {hc.SCANNER_MAP})")
    a = p.parse_args()

    args = list(a.edycje)
    if a.all:
        # pliki zaczynające się od "_" (np. _podsumowanie.csv) to nie tabele edycji
        args += sorted(p for p in glob.glob(os.path.join(a.katalog, "*.csv"))
                       if not os.path.basename(p).startswith("_"))
    if not args:
        p.error("podaj SKU edycji albo --all")

    song_dir = a.katalog_piosenek or hc.hitster_song_dir()
    dysk = os.path.isdir(song_dir)
    if a.tylko_pobrane and not dysk:
        raise SystemExit(f"--tylko-pobrane: katalog piosenek niedostępny ({song_dir}) — podłącz dysk")

    cards = {}
    info = {}      # every card of the tables: [artist, title, year] (the game shows cards without a song too)
    edycje = []
    widziane = set()
    lacznie = collections.Counter()
    print(f"Katalog piosenek: {song_dir}" + ("" if dysk else "  (NIEDOSTĘPNY — dysk niepodłączony?)"))
    print()
    for arg in args:
        sku, rows, path = wczytaj_edycje(arg, a.katalog)
        if sku in widziane:
            continue
        widziane.add(sku)
        edycje.append(sku)
        stat = collections.Counter()
        statusy = collections.Counter()
        brakujace = []
        for r in rows:
            stat["kart"] += 1
            try:
                info[hc.card_key(r.get("sku") or sku, r["karta"])] = [
                    (r.get("wykonawca") or "").strip(), (r.get("tytul") or "").strip(), (r.get("rok") or "").strip()]
            except (KeyError, ValueError):
                pass
            status = (r.get("usdb_status") or "").strip()
            folder = (r.get("folder") or "").strip()
            if status not in hc.DO_POBRANIA:
                stat["pominiete_status"] += 1
                statusy[status or "(pusty)"] += 1
                continue
            if not folder:
                stat["bez_folderu"] += 1
                continue
            try:
                key = hc.card_key(r.get("sku") or sku, r["karta"])
            except (KeyError, ValueError):
                stat["zly_numer"] += 1
                continue
            na_dysku = dysk and os.path.isdir(os.path.join(song_dir, folder))
            if dysk and not na_dysku:
                stat["brak_na_dysku"] += 1
                brakujace.append(folder)
                if a.tylko_pobrane:
                    continue
            value = hc.SONG_SUBDIR + "/" + folder
            if key in cards and cards[key] != value:
                print(f"  UWAGA: {key} powtórzony z innym folderem: {cards[key]!r} -> {value!r}")
            cards[key] = value
            stat["w_mapie"] += 1
        lacznie.update(stat)

        rel = os.path.relpath(path)
        print(f"{sku}  ({path if rel.startswith('..') else rel})")
        print(f"  kart w tabeli:              {stat['kart']}")
        print(f"  w mapie:                    {stat['w_mapie']}")
        pomin = ", ".join(f"{s}: {n}" for s, n in statusy.most_common())
        print(f"  pominięte (status):         {stat['pominiete_status']}" + (f"  [{pomin}]" if pomin else ""))
        if stat["bez_folderu"]:
            print(f"  pominięte (pusty folder):   {stat['bez_folderu']}")
        if stat["zly_numer"]:
            print(f"  pominięte (zły numer):      {stat['zly_numer']}")
        if dysk:
            dop = " (pominięte, --tylko-pobrane)" if a.tylko_pobrane else " (są w mapie)"
            print(f"  brak folderu na dysku:      {stat['brak_na_dysku']}" + (dop if stat["brak_na_dysku"] else ""))
            for f_ in brakujace[:5]:
                print(f"      {f_}")
            if len(brakujace) > 5:
                print(f"      ... i {len(brakujace) - 5} więcej")
        else:
            print("  brak folderu na dysku:      ? (dysk niepodłączony)")
        print()

    data = {"generated": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
            "editions": edycje,
            "cards": dict(sorted(cards.items())),
            "info": dict(sorted(info.items()))}
    out = os.path.abspath(a.output)
    hc.save_json(out, data)

    print(f"Razem: {len(cards)} kart w mapie z {len(edycje)} edycji; "
          f"pominięte (status) {lacznie['pominiete_status']}, "
          + (f"brak folderu na dysku {lacznie['brak_na_dysku']}" if dysk else "dysk niepodłączony"))
    print(f"Zapisano: {out}")


if __name__ == "__main__":
    main()
