#!/usr/bin/env python3
"""Buduje tabele edycji: baza Jumbo -> Spotify -> rok MusicBrainz -> dopasowanie USDB.

  python3 build_edition.py aaae0001 [aaae0005 ...]
  python3 build_edition.py --all                 wszystkie edycje aaae*
  python3 build_edition.py --wszystkie           wszystkie edycje z bazy (najpierw aaae*, potem reszta)
  python3 build_edition.py aaae0001 --bez-mb     bez MusicBrainz (szybko, rok tylko ze Spotify)
  python3 build_edition.py --wszystkie --log .cache/nocny.log --cicho    przebieg nocny

Wynik: editions/<sku>.csv (kolumny: karta, sku, spotify, wykonawca, tytul, rok, rok_spotify,
rok_mb, do_sprawdzenia, usdb_status, usdb_id, folder), editions/<sku>.raport.txt
i editions/_podsumowanie.csv (jedna linia na edycję).

Rok: wcześniejszy z (Spotify, MusicBrainz). Różnica > 1 rok albo brak roku MB -> do_sprawdzenia.
Status USDB: zielony / inna wersja (potwierdzona w overrides.csv) / inna wersja? (kandydat:
ten sam tytuł, inny wykonawca) / czerwony / nieobslugiwany (decyzja w overrides.csv).

Spotify (~0,65 s na utwór) pobiera osobny wątek z wyprzedzeniem, MusicBrainz (1 zapytanie/s)
idzie edycja po edycji. Wszystko cache'owane w .cache/, więc przerwany przebieg wznawia się
od miejsca przerwania (gotowe pozycje są pomijane). ID, które się nie udały, trafiają do
.cache/nieudane_spotify.txt i .cache/nieudane_mb.txt; ponowienie = to samo polecenie jeszcze raz.
"""

import argparse
import collections
import csv
import datetime
import os
import sys
import threading
import time

import hitster_cards as hc

LOG_FILE = None


def log(msg):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line, flush=True)
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def fmt_artists(artists):
    artists = [a for a in artists if a]
    if len(artists) <= 1:
        return "".join(artists)
    if len(artists) == 2:
        return f"{artists[0]} & {artists[1]}"
    return ", ".join(artists[:-1]) + " & " + artists[-1]


class SpotifyPrefetch(threading.Thread):
    """Pobiera metadane Spotify dla listy ID (w kolejności edycji) w tle.
    Jedyny wątek, który zapisuje cache Spotify."""

    def __init__(self, sp, ids, refetch_errors=False):
        super().__init__(daemon=True)
        self.sp, self.ids, self.refetch = sp, ids, refetch_errors
        self.done_ids = set()
        self.finished = threading.Event()
        self.cond = threading.Condition()

    def run(self):
        t0 = time.monotonic()
        todo = [i for i in self.ids if i not in self.sp.data or (self.refetch and "error" in self.sp.data[i])]
        with self.cond:
            self.done_ids = set(self.ids) - set(todo)
            self.cond.notify_all()
        log(f"Spotify: {len(self.ids)} utworów, w cache {len(self.ids) - len(todo)}, do pobrania {len(todo)}")
        try:
            for n, tid in enumerate(todo, 1):
                self.sp.get(tid, self.refetch)
                with self.cond:
                    self.done_ids.add(tid)       # także nieudane: wątek główny nie czeka w nieskończoność
                    self.cond.notify_all()
                if n % 500 == 0 or n == len(todo):
                    el = time.monotonic() - t0
                    log(f"Spotify: {n}/{len(todo)}, nieudane {len(self.sp.failed)}, "
                        f"{el / 60:.0f} min, zostało ~{el / n * (len(todo) - n) / 60:.0f} min")
        finally:
            self.sp.save()
            self.finished.set()
            with self.cond:
                self.cond.notify_all()

    def wait_for(self, ids):
        ids = set(ids)
        with self.cond:
            while not ids <= self.done_ids and not self.finished.is_set():
                self.cond.wait(timeout=5)


def card_row(sku, c, sp, mb, catalog, overrides, use_mb):
    num = int(c["CardNumber"])
    tid = c["Spotify"]
    m = sp.data.get(tid) or {"error": "nie pobrano (do ponowienia)"}
    row = dict.fromkeys(hc.CSV_COLUMNS, "")
    row.update(karta=num, sku=sku, spotify=tid)
    uwagi = []
    if "error" in m:
        uwagi.append(f"Spotify: {m['error']}")
        artists, title = [], ""
    else:
        artists, title = m["artists"], hc.clean_spotify_title(m["name"])
        row["wykonawca"] = fmt_artists(artists)
        row["tytul"] = title
        rs = m.get("release", "")[:4]
        row["rok_spotify"] = rs
        rmb = None
        if use_mb and artists:
            rmb = (mb.get(artists[0], m["name"]) or {}).get("year")
        row["rok_mb"] = rmb or ""
        years = [int(y) for y in (rs, rmb) if y]
        row["rok"] = min(years) if years else ""
        if rs and rmb and abs(int(rs) - int(rmb)) > 1:
            row["do_sprawdzenia"] = "tak"
        elif use_mb and not rmb:
            row["do_sprawdzenia"] = "brak_mb"
    status, rec = catalog.match(artists, title) if title else (hc.CZERWONY, None)

    ov = overrides.get(hc.card_key(sku, num)) or overrides.get(tid) or {}
    if ov.get("usdb_id"):
        rec = catalog.get(ov["usdb_id"])
        if rec is None:
            uwagi.append(f"overrides: brak usdb_id {ov['usdb_id']} w katalogu")
        status = ov.get("usdb_status") or hc.ZIELONY
    elif ov.get("usdb_status"):
        status = ov["usdb_status"]
        if status in (hc.CZERWONY, hc.NIEOBSLUGIWANY):
            rec = None
    for col in ("wykonawca", "tytul", "rok"):
        if ov.get(col):
            row[col] = ov[col]
            if col == "rok":
                row["do_sprawdzenia"] = ""
    row["usdb_status"] = status
    if rec:
        row["usdb_id"] = rec["id"]
        row["folder"] = ov.get("folder") or hc.usdb_folder(rec)
    row["_usdb"] = f"{rec['artist']} - {rec['title']}" if rec else ""
    row["_uwagi"] = "; ".join(uwagi + ([ov["uwagi"]] if ov.get("uwagi") else []))
    return row


SUMMARY_COLS = ["sku", "jezyk", "nazwa", "kart", "zielony", "inna wersja", "inna wersja?",
                "czerwony", "nieobslugiwany", "do_sprawdzenia", "brak_mb", "bez_spotify", "zbudowano"]


def write_summary_line(sku, g, rows):
    path = os.path.join(hc.EDITIONS_DIR, "_podsumowanie.csv")
    old = []
    if os.path.exists(path):
        with open(path, encoding="utf-8", newline="") as f:
            old = [r for r in csv.DictReader(f) if r["sku"] != sku]
    cnt = collections.Counter(r["usdb_status"] for r in rows)
    gd = g["gameset_data"]
    line = {"sku": sku, "jezyk": gd.get("gameset_language", ""), "nazwa": gd.get("gameset_name", ""),
            "kart": len(rows),
            **{s: cnt.get(s, 0) for s in (hc.ZIELONY, hc.INNA_WERSJA, hc.INNA_WERSJA_KANDYDAT,
                                          hc.CZERWONY, hc.NIEOBSLUGIWANY)},
            "do_sprawdzenia": sum(r["do_sprawdzenia"] == "tak" for r in rows),
            "brak_mb": sum(r["do_sprawdzenia"] == "brak_mb" for r in rows),
            "bez_spotify": sum(not r["wykonawca"] for r in rows),
            "zbudowano": f"{datetime.datetime.now():%Y-%m-%d %H:%M}"}
    rows_out = sorted(old + [line], key=lambda r: (not r["sku"].startswith("aaae"), r["sku"]))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SUMMARY_COLS)
        w.writeheader()
        w.writerows(rows_out)
    return line


def save_failed(sp, mb):
    for name, items in (("nieudane_spotify.txt", sp.failed), ("nieudane_mb.txt", mb.failed)):
        path = os.path.join(hc.CACHE_DIR, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(f"{x}\n" for x in sorted(items)))


def build(skus, use_mb, catalog_path, refetch_errors=False, quiet=False):
    db = hc.load_jumbo_db()
    sets = hc.gamesets_by_sku(db)
    for sku in skus:
        if sku not in sets:
            raise SystemExit(f"Nie ma edycji {sku} w bazie Jumbo (fetch_jumbo_db.py --list).")
    catalog = hc.UsdbCatalog(catalog_path)
    overrides = hc.load_overrides()
    sp = hc.SpotifyCache(log=log)
    mb = hc.MBCache(log=log)
    hc.ensure_dir(hc.EDITIONS_DIR)

    ids = list(dict.fromkeys(c["Spotify"] for sku in skus for c in sets[sku]["gameset_data"]["cards"]))
    pre = SpotifyPrefetch(sp, ids, refetch_errors)
    pre.start()
    t_all = time.monotonic()
    log(f"start: edycji {len(skus)}, kart {sum(len(sets[s]['gameset_data']['cards']) for s in skus)}, "
        f"MusicBrainz {'tak' if use_mb else 'nie'}")
    try:
        for i, sku in enumerate(skus, 1):
            g = sets[sku]
            cards = sorted(g["gameset_data"]["cards"], key=lambda c: int(c["CardNumber"]))
            pre.wait_for(c["Spotify"] for c in cards)
            t0 = time.monotonic()
            rows = [card_row(sku, c, sp, mb, catalog, overrides, use_mb) for c in cards]
            mb.save()
            path = os.path.join(hc.EDITIONS_DIR, f"{sku}.csv")
            # zapis atomowy: skaner.py czyta te tabele na żywo i nie może trafić na pół pliku
            with open(path + ".tmp", "w", encoding="utf-8", newline="") as f:
                w = csv.DictWriter(f, fieldnames=hc.CSV_COLUMNS, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            os.replace(path + ".tmp", path)
            report = make_report(sku, g, rows, use_mb)
            with open(os.path.join(hc.EDITIONS_DIR, f"{sku}.raport.txt"), "w", encoding="utf-8") as f:
                f.write(report)
            s = write_summary_line(sku, g, rows)
            save_failed(sp, mb)
            if not quiet:
                print(report)
            log(f"[{i}/{len(skus)}] {sku} {s['nazwa'][:45]}: kart {s['kart']}, zielone {s['zielony']}, "
                f"inna wersja? {s['inna wersja?']}, czerwone {s['czerwony']}, lata do spr. {s['do_sprawdzenia']}, "
                f"brak MB {s['brak_mb']}, bez Spotify {s['bez_spotify']} ({time.monotonic() - t0:.0f} s; "
                f"nieudane: Spotify {len(sp.failed)}, MB {len(mb.failed)})")
    finally:
        mb.save()
        save_failed(sp, mb)
    pre.finished.wait()
    log(f"KONIEC: edycji {len(skus)} w {(time.monotonic() - t_all) / 3600:.1f} h; "
        f"nieudane Spotify {len(sp.failed)}, MB {len(mb.failed)} (.cache/nieudane_*.txt). "
        f"Podsumowanie: {os.path.join(hc.EDITIONS_DIR, '_podsumowanie.csv')}")


def make_report(sku, g, rows, use_mb):
    gd = g["gameset_data"]
    cnt = collections.Counter(r["usdb_status"] for r in rows)
    out = [f"=== {sku}  {gd['gameset_name']} ({gd['gameset_language']}), kart: {len(rows)}",
           "USDB: " + ", ".join(f"{s} {cnt.get(s, 0)}" for s in
                                (hc.ZIELONY, hc.INNA_WERSJA, hc.INNA_WERSJA_KANDYDAT,
                                 hc.CZERWONY, hc.NIEOBSLUGIWANY)),
           f"do pobrania (zielony + inna wersja): {sum(cnt.get(s, 0) for s in hc.DO_POBRANIA)}"]
    for status in (hc.INNA_WERSJA, hc.INNA_WERSJA_KANDYDAT, hc.CZERWONY, hc.NIEOBSLUGIWANY):
        sel = [r for r in rows if r["usdb_status"] == status]
        if not sel:
            continue
        out.append(f"\n-- {status} ({len(sel)})")
        for r in sel:
            extra = f"  -> USDB {r['usdb_id']}: {r['_usdb']}" if r["_usdb"] else ""
            note = f"  [{r['_uwagi']}]" if r["_uwagi"] else ""
            out.append(f"{r['karta']:>5}  {r['wykonawca']} - {r['tytul']} ({r['rok']}){extra}{note}")
    if use_mb:
        chk = [r for r in rows if r["do_sprawdzenia"] == "tak"]
        nomb = [r for r in rows if r["do_sprawdzenia"] == "brak_mb"]
        out.append(f"\n-- lata do sprawdzenia: różnica Spotify/MB > 1 rok: {len(chk)}, brak roku MB: {len(nomb)}")
        for r in chk:
            out.append(f"{r['karta']:>5}  {r['wykonawca']} - {r['tytul']}: Spotify {r['rok_spotify']}, "
                       f"MB {r['rok_mb']} -> {r['rok']}")
        for r in nomb:
            out.append(f"{r['karta']:>5}  {r['wykonawca']} - {r['tytul']}: tylko Spotify {r['rok_spotify']}")
    return "\n".join(out) + "\n"


def main():
    global LOG_FILE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sku", nargs="*")
    ap.add_argument("--all", action="store_true", help="wszystkie edycje o prefiksie --prefix")
    ap.add_argument("--prefix", default="aaae")
    ap.add_argument("--wszystkie", action="store_true", help="wszystkie edycje z bazy, najpierw aaae*")
    ap.add_argument("--bez-mb", action="store_true", help="pomiń MusicBrainz")
    ap.add_argument("--usdb-katalog", default=hc.DEFAULT_USDB_CATALOG)
    ap.add_argument("--ponow-bledy", action="store_true", help="ponów utwory z trwałym błędem Spotify (404)")
    ap.add_argument("--log", help="dopisuj log postępu do pliku")
    ap.add_argument("--cicho", action="store_true", help="nie wypisuj pełnych raportów edycji")
    a = ap.parse_args()
    LOG_FILE = a.log
    skus = [s.lower() for s in a.sku]
    all_skus = sorted(hc.gamesets_by_sku(hc.load_jumbo_db()))
    if a.all:
        skus += [k for k in all_skus if k.startswith(a.prefix)]
    if a.wszystkie:
        skus += [k for k in all_skus if k.startswith("aaae")] + [k for k in all_skus if not k.startswith("aaae")]
    if not skus:
        ap.error("podaj SKU, --all albo --wszystkie")
    build(list(dict.fromkeys(skus)), not a.bez_mb, a.usdb_katalog, a.ponow_bledy, a.cicho)


if __name__ == "__main__":
    sys.exit(main())
