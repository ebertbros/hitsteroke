#!/usr/bin/env python3
"""Demon skanera kart Hitster: kod QR z karty -> PLAY piosenki w grze (UDP 127.0.0.1:47700).

Czyta skaner Honeywell Genesis XP 7680g NIEZALEŻNIE od aktywnego okna, zamienia URL z karty
(https://www.hitstergame.com/<język>/<SKU>/<numer>) na klucz 'sku/numer', szuka folderu
w scanner_map.json (build_scanner_map.py) i wysyła do gry `PLAY hitster/<folder>`.
Karta z bazy, ale bez piosenki dzisiaj (brak w mapie albo folderu na dysku) ->
`UNAVAILABLE wykonawca|tytuł|rok`: gra pokazuje kartę z „Not on today's playlist”.
Nieznana karta (inny format QR albo brak w tabelach) -> `PLAY hitster/__nieznana_karta__`:
świadomie nieistniejący folder, gra odpowiada NOT_FOUND i sama pokazuje
„Try another card” przez 4 s.

WAŻNE: skaner NIE może pracować w trybie klawiatury (HID keyboard, ustawienie fabryczne).
Wtedy „wpisuje” URL i Enter do aktywnego okna, czyli do gry, co uruchamia przypadkową
piosenkę z listy. Ten tryb nie jest obsługiwany. Skaner trzeba raz przełączyć, skanując
kod programujący z instrukcji (Honeywell Genesis XP 7680g User Guide, rozdział
„Interface”/„USB”):
  * USB Serial Emulation (CDC-ACM) — ZALECANY, działa bez instalowania czegokolwiek.
    Kod wg mojej wiedzy: TRMUSB130. Na macOS pojawi się urządzenie /dev/cu.usbmodem*.
  * USB HID POS — wymaga modułu Pythona `hid` (hidapi).
    Kod wg mojej wiedzy: PAP131 (Plug and Play) albo TRMUSB131.
  Powyższe kody TRZEBA sprawdzić w User Guide skanera przed użyciem (różne wersje
  firmware/instrukcji). Sufiks CR (Enter) jest opcjonalny: demon kończy kod na \\r/\\n
  albo po ~150 ms ciszy.

Backendy:
  --serial [ŚCIEŻKA]  USB Serial Emulation (domyślny). Bez ścieżki: autowykrywanie urządzenia
                      /dev/cu.usbmodem* należącego do Honeywell (VID 0x0C2E, przez ioreg).
                      Po odłączeniu skanera demon czeka i otwiera go ponownie (co 2 s).
  --hid               USB HID POS przez opcjonalny moduł `hid` (hidapi).
  --stdin             jedna linia = jeden skan (testy bez skanera).

Przykłady:
  python3 skaner.py                         # serial, autowykrywanie
  python3 skaner.py --serial /dev/cu.usbmodem1101 --log ~/skaner.log
  echo https://www.hitstergame.com/pl/aaae0005/00171 | python3 skaner.py --stdin --dry-run

Inne zachowanie: ten sam kod w ciągu --debounce s (domyślnie 3) jest ignorowany; odpowiedź
BUSY (gra wczytuje listę piosenek) -> kilka ponowień co 1 s; brak odpowiedzi gry w 1 s ->
wpis „gra nie odpowiada” i dalsza praca; mapa jest przeładowywana, gdy zmieni się jej mtime.
Tylko biblioteka standardowa Pythona (+ opcjonalnie `hid` dla --hid).
"""

import argparse
import datetime
import glob
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import termios
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hitster_cards as hc  # noqa: E402

DEFAULT_PORT = 47700
NIEZNANA_KARTA = hc.SONG_SUBDIR + "/__nieznana_karta__"   # celowo nieistniejący folder
CISZA_S = 0.15                 # koniec kodu po tylu sekundach bez nowych bajtów
PONOW_OTWARCIE_S = 2.0         # co ile próbować ponownie otworzyć odłączony skaner
HONEYWELL_VID = 0x0C2E
HID_POS_USAGE_PAGE = 0x8C


# ---------------------------------------------------------------- log

class Log:
    def __init__(self, path=None):
        self.f = open(os.path.expanduser(path), "a", encoding="utf-8", buffering=1) if path else None

    def __call__(self, msg):
        line = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "  " + msg
        print(line, flush=True)
        if self.f:
            self.f.write(line + "\n")


# ---------------------------------------------------------------- mapa kart

class MapaKart:
    """scanner_map.json, przeładowywany automatycznie po zmianie mtime."""

    def __init__(self, path, log):
        self.path = path
        self.log = log
        self.mtime = None
        self.cards = {}
        self.odswiez(start=True)

    def odswiez(self, start=False):
        try:
            mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            if self.mtime is not None or start:
                self.log(f"UWAGA: brak mapy {self.path} — każda karta będzie nieznana "
                         "(zbuduj ją: build_scanner_map.py)")
                self.mtime = None
                self.cards = {}
            return
        if mtime == self.mtime:
            return
        try:
            data = hc.load_json(self.path) or {}
            cards = data.get("cards") or {}
            if not isinstance(cards, dict):
                raise ValueError("pole 'cards' nie jest słownikiem")
        except (ValueError, OSError) as e:
            self.log(f"UWAGA: nie da się wczytać mapy {self.path}: {e} — zostaje poprzednia")
            return
        self.cards = cards
        self.mtime = mtime
        self.log(f"{'Wczytano' if start else 'Przeładowano'} mapę: {len(cards)} kart, "
                 f"edycje {', '.join(data.get('editions') or []) or '-'} ({self.path})")

    def folder(self, key):
        self.odswiez()
        return self.cards.get(key) if key else None


# ---------------------------------------------------------------- gra (UDP)

class Gra:
    def __init__(self, port, log, dry_run=False, timeout=1.0, ponowienia_busy=5):
        self.addr = ("127.0.0.1", port)
        self.log = log
        self.dry_run = dry_run
        self.timeout = timeout
        self.ponowienia_busy = ponowienia_busy
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)

    def _wyczysc(self):
        """Usuwa spóźnione odpowiedzi na poprzednie komendy (np. po timeoucie)."""
        self.sock.setblocking(False)
        try:
            while True:
                self.sock.recv(65535)
        except (BlockingIOError, OSError):
            pass
        finally:
            self.sock.settimeout(self.timeout)

    def wyslij(self, cmd):
        """-> dict z odpowiedzią albo None (brak odpowiedzi)."""
        self._wyczysc()
        try:
            self.sock.sendto(cmd.encode("utf-8"), self.addr)
            data, _ = self.sock.recvfrom(65535)
        except (socket.timeout, ConnectionRefusedError, OSError):
            return None
        try:
            return json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return {"ok": False, "error": "BAD_REPLY", "raw": data[:200].decode("utf-8", "replace")}

    def play(self, folder):
        return self.komenda("PLAY " + folder)

    def niedostepna(self, karta):
        """Karta znana z bazy Jumbo, ale bez piosenki dzisiaj: gra pokazuje kartę
        z komunikatem "Not on today's playlist" (komenda UNAVAILABLE)."""
        pola = (karta.get("wykonawca", ""), karta.get("tytul", ""), karta.get("rok", ""))
        return self.komenda("UNAVAILABLE " + "|".join(p.replace("|", "/").strip() for p in pola))

    def komenda(self, cmd):
        """-> odpowiedź gry (dict) albo None."""
        if self.dry_run:
            self.log(f"  [dry-run] wysłałbym: {cmd}")
            return None
        for proba in range(self.ponowienia_busy + 1):
            rep = self.wyslij(cmd)
            if rep is None:
                self.log(f"  {cmd} -> gra nie odpowiada (port {self.addr[1]}, timeout {self.timeout:g} s)")
                return None
            if rep.get("error") == "BUSY" and proba < self.ponowienia_busy:
                self.log(f"  {cmd} -> BUSY (lista piosenek się ładuje), ponawiam za 1 s "
                         f"[{proba + 1}/{self.ponowienia_busy}]")
                time.sleep(1.0)
                continue
            self.log(f"  {cmd} -> {json.dumps(rep, ensure_ascii=False)}")
            return rep
        return None


class Karty:
    """Dane kart (wykonawca, tytuł, rok) z tabel editions/<sku>.csv, także kart bez
    piosenki: żeby gra mogła pokazać, jaka to karta. Tabele wczytywane leniwie,
    ponownie po zmianie pliku."""

    def __init__(self):
        self.tabele = {}   # sku -> (mtime, {numer: wiersz})

    def __call__(self, key):
        if not key:
            return None
        sku, _, numer = key.partition("/")
        path = os.path.join(hc.EDITIONS_DIR, sku + ".csv")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        if sku not in self.tabele or self.tabele[sku][0] != mtime:
            try:
                rows = {int(r["karta"]): r for r in hc.read_edition_csv(sku)}
            except (OSError, ValueError, KeyError):
                return None
            self.tabele[sku] = (mtime, rows)
        return self.tabele[sku][1].get(int(numer))


# ---------------------------------------------------------------- obsługa skanu

class Obsluga:
    def __init__(self, mapa, gra, log, debounce):
        self.mapa = mapa
        self.gra = gra
        self.log = log
        self.debounce = debounce
        self.ostatni = None
        self.ostatni_t = -1e9
        self.karty = Karty()

    def __call__(self, kod):
        kod = kod.strip().strip("\x00")
        if not kod:
            return
        teraz = time.monotonic()
        if kod == self.ostatni and teraz - self.ostatni_t < self.debounce:
            self.log(f"skan {kod!r}: pominięty (ten sam kod {teraz - self.ostatni_t:.1f} s temu)")
            return
        self.ostatni, self.ostatni_t = kod, teraz
        key = hc.parse_qr(kod)
        folder = self.mapa.folder(key)
        karta = self.karty(key)
        if key is None:
            self.log(f"skan {kod!r}: nieznany format kodu -> nieznana karta")
        elif folder is None and karta is not None:
            self.log(f"skan {kod!r}: klucz {key} bez piosenki -> niedostępna dzisiaj")
            self.gra.niedostepna(karta)
            return
        elif folder is None:
            self.log(f"skan {kod!r}: klucz {key} nie ma w mapie -> nieznana karta")
        else:
            self.log(f"skan {kod!r}: klucz {key} -> {folder}")
        rep = self.gra.play(folder or NIEZNANA_KARTA)
        # w mapie, ale folderu nie ma na dysku (jeszcze nie pobrana)
        if folder and karta is not None and rep and rep.get("error") == "NOT_FOUND":
            self.gra.niedostepna(karta)


class Skladacz:
    """Składa bajty w kody: koniec na \\r/\\n albo po CISZA_S ciszy."""

    def __init__(self):
        self.buf = bytearray()
        self.ostatni_bajt = 0.0

    def dodaj(self, data):
        out = []
        self.buf += data
        self.ostatni_bajt = time.monotonic()
        while True:
            m = re.search(rb"[\r\n]", self.buf)
            if not m:
                break
            kawalek = bytes(self.buf[:m.start()])
            del self.buf[:m.end()]
            if kawalek.strip():
                out.append(kawalek)
        return [k.decode("utf-8", "replace") for k in out]

    def po_ciszy(self):
        if self.buf and time.monotonic() - self.ostatni_bajt >= CISZA_S:
            k = bytes(self.buf)
            self.buf.clear()
            return k.decode("utf-8", "replace") if k.strip() else None
        return None

    def czysc(self):
        self.buf.clear()


# ---------------------------------------------------------------- backend: stdin

def czytaj_stdin(log):
    log("Backend: stdin (jedna linia = jeden skan, koniec: EOF / Ctrl+D)")
    for line in sys.stdin:
        if line.strip():
            yield line.rstrip("\r\n")


# ---------------------------------------------------------------- backend: USB Serial (CDC-ACM)

def honeywell_serial_paths():
    """Ścieżki /dev/cu.* urządzeń Honeywell (VID 0x0C2E) wg ioreg (macOS).
    Heurystyka: najbliższe 'idVendor' przed 'IOCalloutDevice' w drzewie ioreg należy
    do urządzenia/interfejsu USB, z którego powstał port szeregowy.
    -> lista ścieżek albo None, gdy ioreg niedostępny."""
    try:
        out = subprocess.run(["ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-w0"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    paths, vendor = [], None
    for line in out.splitlines():
        if line.startswith("+-o"):
            vendor = None
        m = re.search(r'"idVendor" = (\d+)', line)
        if m:
            vendor = int(m.group(1))
            continue
        m = re.search(r'"IOCalloutDevice" = "([^"]+)"', line)
        if m and vendor == HONEYWELL_VID:
            paths.append(m.group(1))
    return paths


def znajdz_port_serial():
    """-> (ścieżka albo None, opis sytuacji do logu)."""
    kandydaci = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*"))
    honeywell = honeywell_serial_paths()
    if honeywell:
        return honeywell[0], f"Honeywell (VID 0x0C2E): {honeywell[0]}"
    if honeywell is None and len(kandydaci) == 1:   # brak ioreg (nie-macOS): jedyny port CDC
        return kandydaci[0], f"jedyny port CDC: {kandydaci[0]}"
    if kandydaci:
        return None, ("brak skanera Honeywell (VID 0x0C2E) wśród portów "
                      f"{', '.join(kandydaci)} — jeśli to on, podaj ścieżkę: --serial ŚCIEŻKA")
    return None, "brak urządzenia /dev/cu.usbmodem* (skaner odłączony albo nie w trybie USB Serial)"


def otworz_serial(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        attrs = termios.tcgetattr(fd)
        # tryb raw: bez echa, bez przetwarzania znaków, 8N1
        attrs[0] = 0                                                   # iflag
        attrs[1] = 0                                                   # oflag
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL        # cflag
        attrs[3] = 0                                                   # lflag
        attrs[4] = attrs[5] = termios.B115200    # prędkość nieistotna dla CDC-ACM, ale ustawiona
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        termios.tcflush(fd, termios.TCIFLUSH)
    except (termios.error, OSError):
        os.close(fd)
        raise
    return fd


def czytaj_serial(log, sciezka=None):
    log("Backend: USB Serial Emulation (CDC-ACM)" + (f", port {sciezka}" if sciezka else ", autowykrywanie"))
    ostatni_komunikat = None
    sk = Skladacz()
    while True:
        path, opis = (sciezka, sciezka) if sciezka else znajdz_port_serial()
        fd = None
        if path:
            try:
                fd = otworz_serial(path)
            except OSError as e:
                opis = f"nie da się otworzyć {path}: {e.strerror or e}"
        if fd is None:
            if opis != ostatni_komunikat:
                log(f"Czekam na skaner: {opis} (ponawiam co {PONOW_OTWARCIE_S:g} s)")
                ostatni_komunikat = opis
            time.sleep(PONOW_OTWARCIE_S)
            continue
        log(f"Skaner podłączony: {path}")
        ostatni_komunikat = None
        sk.czysc()
        try:
            while True:
                r, _, _ = select.select([fd], [], [], CISZA_S if sk.buf else 1.0)
                if r:
                    data = os.read(fd, 4096)
                    if not data:          # select gotowy, 0 bajtów = rozłączenie (hangup)
                        raise OSError("koniec danych (odłączony?)")
                    yield from sk.dodaj(data)
                else:
                    k = sk.po_ciszy()
                    if k:
                        yield k
                    if not os.path.exists(path):
                        raise OSError("urządzenie zniknęło")
        except OSError as e:
            k = sk.po_ciszy() if sk.buf else None
            log(f"Skaner odłączony ({path}: {e.strerror or e}) — ponawiam otwarcie co {PONOW_OTWARCIE_S:g} s")
            if k:
                yield k
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        time.sleep(PONOW_OTWARCIE_S)


# ---------------------------------------------------------------- backend: USB HID POS

def czytaj_hid(log):
    """HID POS przez moduł `hid` (hidapi).

    FORMAT RAPORTU WG DOKUMENTACJI HONEYWELL HID POS — DO SPRAWDZENIA NA FIZYCZNYM SKANERZE:
      bajt 0      ID raportu = 0x02 (raport wejściowy z danymi kodu)
      bajt 1      długość danych w tym raporcie
      bajty 2..   dane kodu (dalej m.in. identyfikator symbologii AIM)
      ostatni     bit0 = 1: „więcej danych” (kod dłuższy niż jeden raport, przyjdzie kolejny)
    Kawałki są sklejane; kod jest gotowy, gdy brak bitu kontynuacji albo po 150 ms ciszy.
    """
    try:
        import hid  # opcjonalny: pip install hid (ctypes, wymaga biblioteki hidapi) albo hidapi
    except Exception as e:   # ImportError albo brak biblioteki natywnej hidapi
        log(f"BŁĄD: --hid wymaga modułu Pythona `hid` (hidapi), którego tu nie ma ({e}).\n"
            "  Zainstaluj go (za zgodą właściciela stanowiska), np.: python3 -m pip install hidapi\n"
            "  albo użyj trybu USB Serial: przełącz skaner na USB Serial Emulation i uruchom z --serial.")
        sys.exit(2)

    log("Backend: USB HID POS (hidapi)")
    ostatni_komunikat = None
    while True:
        devs = hid.enumerate(HONEYWELL_VID, 0)
        pos = [d for d in devs if d.get("usage_page") == HID_POS_USAGE_PAGE]
        if pos:
            info = pos[0]
        else:
            # bez interfejsu HID POS: dowolny poza klawiaturą (usage_page 1, usage 6)
            inne = [d for d in devs if not (d.get("usage_page") == 0x01 and d.get("usage") == 0x06)]
            info = inne[0] if inne else None
            if info and ostatni_komunikat != "bez-0x8c":
                log("UWAGA: brak interfejsu z usage_page 0x8C (HID POS), próbuję "
                    f"usage_page 0x{info.get('usage_page', 0):X} — czy skaner jest w trybie HID POS?")
                ostatni_komunikat = "bez-0x8c"
        if info is None:
            opis = ("skaner Honeywell jest tylko klawiaturą — przełącz go w tryb HID POS"
                    if devs else "brak urządzenia Honeywell HID (VID 0x0C2E)")
            if opis != ostatni_komunikat:
                log(f"Czekam na skaner: {opis} (ponawiam co {PONOW_OTWARCIE_S:g} s)")
                ostatni_komunikat = opis
            time.sleep(PONOW_OTWARCIE_S)
            continue
        try:
            if hasattr(hid, "Device"):            # pakiet `hid` (ctypes)
                dev = hid.Device(path=info["path"])
            else:                                 # pakiet `hidapi` (cython)
                dev = hid.device()
                dev.open_path(info["path"])
        except Exception as e:
            opis = f"nie da się otworzyć urządzenia HID: {e}"
            if opis != ostatni_komunikat:
                log(f"Czekam na skaner: {opis} (ponawiam co {PONOW_OTWARCIE_S:g} s)")
                ostatni_komunikat = opis
            time.sleep(PONOW_OTWARCIE_S)
            continue
        log(f"Skaner podłączony (HID): {info.get('product_string') or ''} "
            f"usage_page 0x{info.get('usage_page', 0):X}")
        ostatni_komunikat = None
        buf = bytearray()
        ostatni_bajt = 0.0
        ostrzezono_id = False
        try:
            while True:
                data = bytes(dev.read(64, int(CISZA_S * 1000)) or b"")
                if not data:
                    if buf and time.monotonic() - ostatni_bajt >= CISZA_S:
                        yield buf.decode("utf-8", "replace")
                        buf.clear()
                    continue
                if data[0] != 0x02 or len(data) < 3:
                    if not ostrzezono_id:
                        log(f"UWAGA: nieoczekiwany raport HID (ID 0x{data[0]:02X}, {len(data)} B): "
                            f"{data[:16].hex()} — sprawdź format HID POS")
                        ostrzezono_id = True
                    continue
                n = min(data[1], len(data) - 3)     # bez bajtu ID, długości i ostatniego bajtu flag
                buf += data[2:2 + n]
                ostatni_bajt = time.monotonic()
                if not (data[-1] & 0x01):           # brak „więcej danych” -> kod kompletny
                    kod = buf.decode("utf-8", "replace")
                    buf.clear()
                    yield kod
        except (OSError, IOError, ValueError) as e:
            log(f"Skaner odłączony (HID: {e}) — ponawiam otwarcie co {PONOW_OTWARCIE_S:g} s")
            if buf:
                yield buf.decode("utf-8", "replace")
        finally:
            try:
                dev.close()
            except Exception:
                pass
        time.sleep(PONOW_OTWARCIE_S)


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--serial", nargs="?", const="auto", metavar="ŚCIEŻKA",
                   help="USB Serial Emulation (domyślnie; bez ścieżki: autowykrywanie)")
    g.add_argument("--hid", action="store_true", help="USB HID POS (wymaga modułu `hid`)")
    g.add_argument("--stdin", action="store_true", help="jedna linia = jeden skan (testy)")
    p.add_argument("--map", default=hc.SCANNER_MAP, help=f"mapa kart (domyślnie {hc.SCANNER_MAP})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port UDP gry (domyślnie {DEFAULT_PORT})")
    p.add_argument("--debounce", type=float, default=3.0,
                   help="ignoruj ten sam kod przez tyle sekund (domyślnie 3)")
    p.add_argument("--busy-ponowienia", type=int, default=5,
                   help="ile razy ponowić PLAY po BUSY, co 1 s (domyślnie 5)")
    p.add_argument("--dry-run", action="store_true", help="nie wysyłaj UDP, tylko loguj komendy")
    p.add_argument("--log", metavar="PLIK", help="dopisuj log także do pliku")
    a = p.parse_args()

    log = Log(a.log)
    log(f"Start skanera: port {a.port}{' (dry-run)' if a.dry_run else ''}, debounce {a.debounce:g} s")
    mapa = MapaKart(os.path.abspath(os.path.expanduser(a.map)), log)
    gra = Gra(a.port, log, dry_run=a.dry_run, ponowienia_busy=a.busy_ponowienia)
    obsluga = Obsluga(mapa, gra, log, a.debounce)

    if a.stdin:
        zrodlo = czytaj_stdin(log)
    elif a.hid:
        zrodlo = czytaj_hid(log)
    else:
        zrodlo = czytaj_serial(log, None if a.serial in (None, "auto") else a.serial)

    # SIGTERM (np. launchd, kill) kończy pracę tak samo jak Ctrl+C
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    try:
        for kod in zrodlo:
            obsluga(kod)
    except KeyboardInterrupt:
        pass
    log("Koniec pracy skanera.")


if __name__ == "__main__":
    main()
