# Wygląd stanowiska Hitster Karaoke

Trzy ekrany stanowiska rysuje `src/base/UHitsterUI.pas` (wywoływany z
`URemoteControl.pas`, gdy w `config.ini` jest `[Remote] Design=1`):

- **oczekiwanie**: karta Hitster z kodem QR i karta z rokiem, „Scan your card”,
  strzałka w stronę skanera. Zasłania listę piosenek i menu startowe.
- **śpiewanie**: pasek pojedynku (P1 różowy z lewej, P2 niebieski z prawej),
  karta piosenki z rokiem i postępem, plakietki P1/P2 przy torach nut.
  Wideo, przyciemnienie, nuty i tekst rysuje gra według motywu `Hitster`.
- **wyniki**: karta piosenki, „NOW PLAYING” (gra fragment piosenki, głośność
  `TeaserVolume`), wyniki liczone od zera, zwycięzca, „Scan your next card”
  i odliczanie do powrotu (`ScoreSeconds`).

Kolory graczy w grze: P1 `#E6007E`, P2 `#00AAD5` (pozycje 6 i 12 palety w
`UThemes.GetPlayerColor`, w `config.ini` `[PlayerColor] P1=6, P2=12`).
Mikrofony na stoisku mają te same kolory.

## Instalacja na komputerze stanowiska

Materiały marki (czcionka BrownStd, okręgi Hitstera) są poufne i nie trafiają
do repozytorium. Skrypt czyta je z osobnego katalogu i buduje wszystko w
katalogu użytkownika gry:

```bash
python3 hitster/install.py --brand ../assets/brand
```

Grę trzeba zamknąć przed uruchomieniem skryptu. Potrzebne są: macOS (`sips`),
Chromium albo Google Chrome, `ffmpeg`, Python 3.

Skrypt tworzy w `~/Library/Application Support/UltraStar Deluxe/`:

| co | gdzie |
|---|---|
| czcionki (BrownStd + Noto jako zapas) | `fonts/fonts.ini` |
| motyw `Hitster` (kopia Modern: ukryte ramki wyników i pasek czasu, przyciemnienie pod nutami, własne nuty) | `themes/Hitster.ini`, `themes/Hitster/` |
| grafiki ekranów | `hitster/*.png` |
| ustawienia (motyw, czcionka, kolory, `ScreenFade=Off`, `SingScores=Off`, `[Remote] Design=1`) | `config.ini` (kopia: `config.ini.bak-hitster`) |

`--no-config` odświeża same grafiki i motyw bez ruszania `config.ini`.
Brak grafik nie psuje gry: ekrany rysują się wtedy samymi kolorami.

## Sprawdzanie

```bash
python3 remote/usdx_remote.py play "Toto - Africa"
python3 remote/usdx_remote.py raw "SHOT /tmp/sing.jpg"
python3 remote/usdx_remote.py raw FINISH
python3 remote/usdx_remote.py raw "SHOT /tmp/score.jpg"
```
