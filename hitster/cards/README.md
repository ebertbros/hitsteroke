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
