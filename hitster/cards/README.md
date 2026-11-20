# Baza kart Hitstera

`gameset_database.json` to baza kart pobrana z serwera Jumbo (wydawcy Hitstera):
<https://hitster.jumboplay.com/hitster-assets/gameset_database.json>.
Tego pliku używa strona hitstergame.com do odtwarzania utworów po skanie karty.

Kopia w repo: pobrana 2026-11-20, pole `updated_on` = 2026-11-19 15:06.
176 edycji z 37 krajów, 51 208 kart, 22 920 różnych utworów.

## Kod QR na karcie

```
https://www.hitstergame.com/<język>/<SKU>/<numer karty>
np.  https://www.hitstergame.com/pl/aaae0005/00171
```

Utwór wyznacza para **SKU + numer karty**. Język w adresie nie jest potrzebny do wyszukania.

## Format pliku

```json
{"updated_on": 1795097161,
 "gamesets": [
   {"sku": "aaae0005",
    "gameset_data": {
      "gameset_language": "Poland",
      "gameset_name": "Hitster Bingo",
      "cards": [{"CardNumber": "00171", "Spotify": "1wZ7o1mZysv1l26ZWawVfW", "ArtistType": "solo"}]}}]}
```

Baza **nie ma wykonawcy, tytułu ani roku**, tylko ID utworu na Spotify
(`ArtistType` = `solo`/`group`, nie w każdej edycji). Nazwy trzeba dobrać z innego źródła po ID Spotify.

## Edycje z regionu (licencjobiorca `aaae`)

| SKU | Kraj | Talia | Kart |
|---|---|---|---|
| aaae0001 | Polska | Hitster Original | 308 |
| aaae0002 | Polska | Hitster Original (2nd Edition) | 308 |
| aaae0003 | Węgry | Hitster Original | 308 |
| aaae0004 | Polska | Summer Party | 308 |
| aaae0005 | Polska | Hitster Bingo | 226 |
| aaae0006 | Czechy/Słowacja | Hitster Original (2nd Edition) | 308 |
| aaae0007 | Rumunia | Hitster Original (2nd Edition) | 308 |
| aaae0008 | Polska | Disco Polo | 308 |
| aaae0009 | Polska | Soundtracks (Exp.) | 154 |
| aaae0010 | Polska | Rock | 309 |
| aaae0011 | Węgry | Rock | 308 |
| aaae0012 | Polska | Christmas (Exp.) | 154 |
| aaae0013 | Węgry | Christmas (Exp.) | 154 |
| aaae0014 | Polska | Soundtracks (Exp.) (5 languages) | 154 |
| aaae0015 | Polska | Hitster Bingo (5 languages) | 225 |
| aaae0016 | Polska | Summer Party (5 languages) | 308 |
| aaae0017 | Polska | Hip Hop (Exp.) | 154 |
| aaae0018 | Węgry | Hip Hop (Exp.) | 154 |

Sprawdzone na fizycznych kartach: `aaae0005/00171` = George Michael, Freedom!;
`aaae0005/00149` = Take That, Could It Be Magic.

## Uwaga: arkusze TM Toys

Kolumna `#`/`no` w arkuszach z listami utworów od TM Toys **nie jest numerem karty**
(to numer porządkowy po roku). Do kodu QR używać tylko tej bazy.

## Aktualizacja

```bash
python3 hitster/cards/fetch_jumbo_db.py --check   # co się zmieniło u Jumbo
python3 hitster/cards/fetch_jumbo_db.py           # pobierz i nadpisz
```

Na evencie gra korzysta z kopii z repo, bez internetu. Plik należy do Jumbo; nie jest to oficjalnie udostępnione API
i może się zmienić bez zapowiedzi.

# Tabele kart, pobieranie piosenek i skaner

Skrypty w tym katalogu (biblioteka standardowa Pythona 3.10+, do pobierania `yt-dlp` i `ffmpeg`).

## Skąd dane

- **Karta → utwór**: baza Jumbo `gameset_database.json` (SKU edycji, numer karty, ID Spotify).
  Kod QR na karcie: `https://www.hitstergame.com/<język>/<SKU>/<numer>`; język pomijamy,
  inne formaty to nieznana karta. Klucz karty w tabelach i mapie: `aaae0005/00171`.
- **Wykonawca, tytuł, rok**: strona `open.spotify.com/embed/track/<ID>` (nieoficjalnie, bez klucza,
  ~0,4 s na utwór, cache w `.cache/spotify.json`). Dopiski typu „- Remastered 2011” są usuwane.
- **Drugi rok**: MusicBrainz (najwcześniejszy `first-release-date` nagrania tego wykonawcy
  o tym tytule, 1 zapytanie/s, cache w `.cache/musicbrainz.json`). Do tabeli trafia wcześniejszy
  z dwóch lat; różnica > 1 rok albo brak roku MB = `do_sprawdzenia`.
- **Nuty**: katalog USDB (`~/Downloads/hitster-usdx/usdb_katalog.json`, opcja `--usdb-katalog`).

Arkuszy TM Toys nie używamy (ich numeracja nie zgadza się z kartami).

## Kolejność

```bash
cd hitster/cards
python3 fetch_jumbo_db.py --check         # czy Jumbo coś zmieniło (kopia w repo zostaje)
python3 fetch_jumbo_db.py --list aaae     # edycje z lokalnej kopii
python3 build_edition.py aaae0001 aaae0005    # tabele editions/<sku>.csv + raporty
python3 build_edition.py --wszystkie --log .cache/nocny.log --cicho   # wszystkie 176 edycji (~8 h)
python3 usdb_login.py                     # raz, SAM w terminalu: logowanie do USDB
python3 pobierz.py aaae0001 aaae0005      # pobieranie piosenek na dysk gry
python3 build_scanner_map.py aaae0001 aaae0005   # mapa skanera scanner_map.json
python3 skaner.py --serial                # demon skanera (opis w docstringu skaner.py)
```

## Tabela edycji `editions/<sku>.csv`

| kolumna | znaczenie |
|---|---|
| `karta`, `sku`, `spotify` | numer karty, edycja, ID Spotify z bazy Jumbo |
| `wykonawca`, `tytul` | ze Spotify (oczyszczone) |
| `rok` | wcześniejszy z `rok_spotify` i `rok_mb` (albo ręczny z overrides.csv) |
| `do_sprawdzenia` | `tak` (lata różnią się o > 1), `brak_mb` (tylko Spotify), puste |
| `usdb_status` | `zielony`, `inna wersja`, `inna wersja?`, `czerwony`, `nieobslugiwany` |
| `usdb_id`, `folder` | plik USDB i folder piosenki (`Wykonawca - Tytuł` z USDB) |

Statusy:

- `zielony`: w USDB są nuty tego utworu tego wykonawcy.
- `inna wersja?`: ten sam tytuł u innego wykonawcy. Często to inny utwór („Love Train”
  Holly Johnson zamiast The O'Jays), więc to tylko kandydat. Nie jest pobierany.
- `inna wersja`: cover tej samej piosenki potwierdzony w `overrides.csv` (pobierany).
- `czerwony`: brak nut w USDB.
- `nieobslugiwany`: rap, instrumental, opera itp. (decyzja w `overrides.csv`).

Raport `editions/<sku>.raport.txt` wylicza karty niezielone i lata do sprawdzenia.

## Ręczne decyzje `overrides.csv`

Kolumny: `klucz, usdb_status, usdb_id, rok, wykonawca, tytul, folder, uwagi`.
`klucz` to ID Spotify (decyzja dla tego utworu we wszystkich edycjach) albo `sku/numer`
(jedna karta). Puste pola nie zmieniają wyniku automatu. Po zmianie: `build_edition.py`
(z cache trwa sekundy).

## Pobieranie (`pobierz.py`)

Dla statusów `zielony` i `inna wersja`. `.txt` z USDB (cache `.cache/usdb_txt/`), z `#VIDEO`:
`v=` teledysk mp4 do 720p + mp3, `a=` samo audio, `co=` okładka z fanart.tv. Audio
znormalizowane do −14 LUFS (loudnorm, dwa przebiegi). W `.txt`: lokalne `#MP3/#AUDIO/#VIDEO/#COVER`,
`#YEAR` = rok z tabeli, `#PREVIEWSTART` z fragmentu medley USDB albo z pierwszej najczęściej
powtarzanej linii, jeśli USDB go nie podaje. Wszystko na dysk gry (`SongDir1` z config.ini, podkatalog
`hitster/`), także pliki tymczasowe. Raport błędów: `.cache/raporty/pobierz_<data>.csv`.
Opcje: `--karty 1,2,3`, `--limit N`, `--dry-run`, `--force`, `--szukaj-zamiennika`.

USDB wymaga konta: `usdb_login.py` pyta o login i hasło w terminalu i zapisuje tylko ciasteczko
sesji (`.cache/usdb_cookies.txt`, prawa 600).

## Czego nie commitować

Repo jest publiczne. W `.gitignore` są: `.cache/` (cache Spotify/MusicBrainz/USDB, sesja USDB,
raporty), `editions/` (tabele kart) i `scanner_map.json`. Do repo trafia tylko kopia bazy Jumbo
`gameset_database.json` (potrzebna offline na evencie) i skrypty. Piosenki leżą poza repo, na dysku gry. Materiały marki Hitster nie trafiają do repo.
