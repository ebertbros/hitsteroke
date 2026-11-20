# Pilot USDX (Hitster karaoke)

Lokalny kanał komend, przez który zewnętrzny skrypt (np. obsługa skanera kart)
steruje grą UltraStar Deluxe. Kod: `src/base/URemoteControl.pas`.

## Włączenie

W `config.ini` (macOS: `~/Library/Application Support/UltraStar Deluxe/config.ini`)
dodaj sekcję `[Remote]`. Pełny przykład jest w [`config.example.ini`](config.example.ini).

```ini
[Remote]
Enabled=1
Port=47700
SkipScore=0
ScoreSeconds=15
Players=2
WaitingText=Scan your card
WaitingSubText=
```

`Enabled=0` (albo brak sekcji) oznacza, że gra działa dokładnie jak upstream:
nie otwiera gniazda i nie zmienia żadnego zachowania.

Po starcie w trybie eventowym gra sama przechodzi z menu głównego przez ekran
graczy (zatwierdza liczbę graczy z `Players`) na ekran wyboru piosenki.
To jest ekran oczekiwania z napisem `WaitingText`.

Po piosence gra pokazuje ekran wyników i po `ScoreSeconds` sekundach
(domyślnie 15) sama wraca na ekran oczekiwania. Jeśli ktoś wciśnie Enter,
pojawi się tabela rekordów, która też wraca sama po `ScoreSeconds`.
`ScoreSeconds=0` oznacza zachowanie upstream (ekran czeka na klawisz),
`SkipScore=1` pomija wyniki całkiem. STOP w trakcie piosenki zawsze pomija
wyniki. PLAY na ekranie wyników od razu uruchamia nową piosenkę.

Wygląd stanowiska (ekran oczekiwania, pasek pojedynku, ekran wyników
z zajawką piosenki) rysuje `src/base/UHitsterUI.pas`, gdy `Design=1`.
Grafiki, motyw i czcionki instaluje `hitster/install.py`, patrz
[`hitster/README.md`](../hitster/README.md).

`Logos=1` pokazuje w prawym górnym rogu ekranu oczekiwania i ekranu wyników
logo Hitster i TM Toys (np. na prezentację dla klienta; domyślnie `0`, na
ekranie śpiewu nigdy). Grafikę `logos.png` tworzy `hitster/install.py`; bez
niej nic się nie rysuje. W trakcie działania gry przełącza to komenda `LOGOS`.

## Protokół

- Transport: UDP, adres `127.0.0.1`, port `Port` (domyślnie 47700).
  Gra słucha tylko na pętli zwrotnej, z sieci nic się nie połączy.
- Zapytanie: jeden datagram = jedna komenda, tekst UTF-8. Słowo komendy bez
  rozróżniania wielkości liter, reszta linii to argument (spacje dozwolone).
  Końcowy `\n` jest opcjonalny.
- Odpowiedź: jeden datagram z jedną linią JSON (zakończoną `\n`) na adres
  nadawcy. Każda odpowiedź ma pola `ok` (bool) i `cmd`.
- Gra czyta komendy raz na klatkę w głównym wątku (do 32 na klatkę),
  odpowiedź przychodzi zwykle w kilkanaście ms.

### PLAY &lt;folder&gt;

Uruchamia piosenkę z folderu podanego względem dowolnego katalogu z piosenkami
(np. `~/Music/UltraStar Deluxe`, `-SongPath`, `[Directories]`).
Z dowolnego ekranu: kończy bieżącą piosenkę, zamyka popupy i menu, wraca na ekran
wyboru i startuje wskazaną piosenkę.

```
PLAY Toto - Africa
PLAY hitster/Queen - Bohemian Rhapsody
PLAY Toto - Africa/Toto - Africa.txt      # gdy w folderze jest kilka plików .txt
```

Dopasowanie folderu:

1. dokładne,
2. bez rozróżniania wielkości liter,
3. bez znaków diakrytycznych (NFC i NFD traktowane jednakowo).

Poziomy 2 i 3 przyjmują tylko jednoznaczne trafienie.

Odpowiedź przychodzi od razu po przyjęciu komendy. Start piosenki trwa jeszcze
około 0,5–1 s; potwierdzisz go przez `STATUS` (`state: "playing"`, rosnące `starts`).

```json
{"ok":true,"cmd":"PLAY","song":"Toto - Africa","artist":"Toto","title":"Africa"}
{"ok":false,"cmd":"PLAY","error":"NOT_FOUND","song":"Nie ma takiej"}
```

Błędy PLAY:

- `NOT_FOUND`: nie ma takiego folderu. Piosenka się nie zmienia, wpis trafia do
  `Error.log`. Przy `Design=1` ekran oczekiwania (i ekran wyników) pokazuje na
  4 s „Try another card / This song isn't on today's list”; w trakcie
  pojedynku nic się nie wyświetla, żeby przypadkowy skan nie przeszkadzał.
- `AMBIGUOUS`: luźne dopasowanie trafiło w kilka folderów.
- `EMPTY_PATH`: brak argumentu.
- `BUSY`: lista piosenek jeszcze się ładuje (kilka sekund po starcie gry).

Jeśli PLAY przyjdzie w trakcie przełączania, wykona się tylko ostatnie.
Podwójny skan nie uruchomi więc dwóch startów.

Jeśli pliku piosenki nie da się wczytać (uszkodzony .txt, brak audio), gra wraca
na ekran oczekiwania, a `STATUS.last_error` ma postać `LOAD_FAILED: <folder>`.

### STOP

Przerywa piosenkę (bez ekranu wyników) albo zamyka ekran wyników i wraca na
ekran oczekiwania.

```json
{"ok":true,"cmd":"STOP"}
```

### STATUS

```json
{"ok":true,"cmd":"STATUS","screen":"sing","state":"playing",
 "song":"Toto - Africa","artist":"Toto","title":"Africa",
 "time":42.100,"total":271.443,"progress":0.155,"players":2,
 "songs_ready":true,"last_start_ms":468,"starts":12,"last_error":null}
```

| pole | znaczenie |
|---|---|
| `screen` | `song` (oczekiwanie), `sing`, `score`, `highscores`, `main`, `name`, `loading`, `options`, `edit`, `jukebox`, `other` |
| `state` | `idle` (ekran oczekiwania), `playing`, `paused`, `results` (ekran wyników lub rekordów), `switching` (trwa przejście lub wykonanie komendy), `starting` (gra się uruchamia), `other` |
| `song` | folder piosenki uruchomionej przez pilota (pusty, gdy nic nie gra) |
| `artist`, `title` | dane bieżącej piosenki (na ekranie śpiewu) |
| `time`, `total`, `progress` | pozycja audio w s, długość w s, postęp 0..1 (w stanie `switching` mogą jeszcze dotyczyć poprzedniej piosenki) |
| `players` | liczba graczy |
| `songs_ready` | `false`, dopóki lista piosenek się ładuje |
| `last_start_ms` | czas od ostatniego PLAY do pojawienia się ekranu śpiewu |
| `starts` | licznik piosenek uruchomionych przez pilota |
| `last_error` | ostatni błąd (`NOT_FOUND: ...`, `LOAD_FAILED: ...`) albo `null` |

### FINISH

Kończy piosenkę od razu, ale tak, jakby skończyła się sama: z ekranem wyników
(STOP je pomija). Poza ekranem śpiewu zwraca `"error":"NOT_SINGING"`.

### UNAVAILABLE &lt;wykonawca&gt;|&lt;tytuł&gt;|&lt;rok&gt;

Karta znana (z bazy kart), ale jej piosenki nie ma dziś w bibliotece. Przy
`Design=1` gra pokazuje odwracającą się kartę z danymi i komunikat „Not on
today's playlist / Sorry, we can't play this song today. Try another card!”
przez ok. 6 s. W trakcie piosenki zwraca `"error":"SINGING"` i nic nie
pokazuje. Wysyła ją skrypt skanera (`hitster/cards/skaner.py`).

```
UNAVAILABLE Harry Styles|Late Night Talking|2022
```

### Odkrycie karty przed piosenką

Przy `Design=1` i `[Remote] Reveal=1` (domyślnie) PLAY nie startuje piosenki od
razu: na ekranie karta obraca się ze strony z kodem QR na stronę z rokiem,
potem odliczanie 3-2-1 i podpowiedź „P1 Left mic / Right mic P2”. Start
piosenki ok. 3,5 s po skanie. `Reveal=0` wyłącza animację.

### MENU [main|options|record]

Otwiera menu dla operatora: główne, opcje albo ustawienia mikrofonów
(`record`). Ekran oczekiwania zasłania listę piosenek, więc to jest droga do
ustawień bez wychodzenia z gry. W trakcie piosenki zwraca `"error":"SINGING"`
(najpierw STOP). Powrót na ekran oczekiwania: `STOP` albo `PLAY`.

### SHOT [&lt;plik.jpg&gt;]

Zapisuje to, co widać na ekranie (razem z nakładką), do podanego pliku albo do
katalogu `screenshots` gry. Bez okienka „zapisano zrzut”.

```json
{"ok":true,"cmd":"SHOT","file":"/Users/.../screenshots/hitster-20261119-101500.jpg"}
```

### LOGOS on|off

Pokazuje albo chowa logo Hitster i TM Toys na ekranie oczekiwania i ekranie
wyników, od razu i tylko do końca działania gry (nie zapisuje `config.ini`;
wartość przy starcie to `[Remote] Logos`). Inny argument zwraca
`"error":"BAD_ARG"`.

```json
{"ok":true,"cmd":"LOGOS","logos":true}
```

`PING` działa jak `STATUS`. Nieznana komenda zwraca `{"ok":false,...,"error":"BAD_COMMAND"}`.

## Przykład dla skryptu skanera (Python)

```python
import json, socket

sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(1.0)

def usdx(cmd):
    sock.sendto(cmd.encode("utf-8"), ("127.0.0.1", 47700))
    return json.loads(sock.recv(65535))

print(usdx("PLAY Toto - Africa"))
print(usdx("STATUS"))
```

Z powłoki (netcat):

```bash
printf 'PLAY Toto - Africa' | nc -u -w1 127.0.0.1 47700
```

## Narzędzie testowe

```bash
python3 remote/usdx_remote.py status
python3 remote/usdx_remote.py play "Toto - Africa"
python3 remote/usdx_remote.py stop
python3 remote/usdx_remote.py stress 20 "Toto - Africa" "Oasis - Wonderwall"
python3 remote/usdx_remote.py switch 10 "Toto - Africa" "Oasis - Wonderwall"
```

- `stress N` robi N cykli PLAY → gra → STOP → oczekiwanie. Mierzy czas startu
  i pamięć procesu gry (RSS).
- `switch N` wysyła PLAY w trakcie innej piosenki i mierzy czas przełączenia.

## Wyniki testów (Mac mini M4, 18.11.2026, 7 piosenek)

| test | wynik |
|---|---|
| PLAY z ekranu oczekiwania (20 cykli) | 436–533 ms, średnio 495 ms |
| PLAY w trakcie innej piosenki (6×) | 915–959 ms |
| PLAY z menu głównego | 894 ms |
| 20× PLAY/STOP | bez zawieszenia; RSS 105–226 MB, bez trendu wzrostowego |
| `Enabled=0` | brak gniazda UDP, brak wpisów `[Remote]` w logu |

## Ograniczenia

- Pilot działa tylko na systemach uniksowych (macOS, Linux). Na Windows sekcja
  `[Remote]` jest ignorowana, a w logu pojawia się błąd.
- Gdy gracz nie ma przypisanego mikrofonu, upstream przy starcie piosenki
  pokazuje popup „Gracz N nie ma przypisanego mikrofonu”. Piosenka gra dalej,
  a popup zamyka następna komenda pilota. Mikrofony trzeba więc przypisać
  w Opcje → Nagrywanie.
