#!/usr/bin/env python3
"""Instaluje wygląd Hitster Karaoke w katalogu użytkownika USDX.

Buduje lokalnie (nic z tego nie trafia do repozytorium):

  <userdir>/fonts/       fonts.ini + BrownStd + Noto (zapas na brakujące znaki)
  <userdir>/themes/      motyw Hitster (Hitster.ini + skórka Hitster/)
  <userdir>/hitster/     grafiki ekranów: oczekiwanie, karty, pasek pojedynku
  <userdir>/config.ini   klucze motywu, czcionki, kolorów i trybu eventowego

Czcionki, motyw i config.ini (ok. 65 MB) zostają w katalogu użytkownika: bez
nich gra nie wystartuje. Dane, które zajmują miejsce, można trzymać gdzie
indziej, np. na dysku zewnętrznym:

  --data DIR    grafiki w DIR/art, biblioteka piosenek w DIR/songs
  --songs DIR   inna lokalizacja samej biblioteki piosenek

  python3 hitster/install.py --brand ../assets/brand --data "/Volumes/DYSK/Hitster Karaoke"

Materiały marki (BrownStd, okręgi Hitstera) są poufne i nie mogą być w publicznym
forku, dlatego skrypt czyta je z osobnego katalogu (--brand).

Wymaga: macOS (sips), Chromium lub Google Chrome (render grafik), ffmpeg, Python 3 bez
dodatkowych pakietów. Grę trzeba zamknąć przed uruchomieniem (nadpisuje config.ini).

  python3 hitster/install.py --brand ../assets/brand
"""

import argparse
import base64
import configparser
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_USERDIR = pathlib.Path.home() / "Library/Application Support/UltraStar Deluxe"
CHROMES = [
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

# Hitster Original style guide 2026 + mic colours agreed for the event
BG = "#00161d"
P1 = "#e6007e"
P2 = "#00aad5"
YELLOW = "#ffce44"

# year-card colours; pink and cyan are left out on purpose, they belong to P1/P2
CARDS = {
    "coral":  ("#ff5f93", "#ff7a6e", "#ff9152"),
    "yellow": ("#ffe066", "#ffce44", "#ffb23a"),
    "lime":   ("#d4f06a", "#b5e255", "#8fd14f"),
    "lilac":  ("#d9b8ff", "#c3a0ff", "#a888ff"),
    "orange": ("#ffb05c", "#ff9a3d", "#ff7f2a"),
}


def die(msg):
    print("BŁĄD: " + msg, file=sys.stderr)
    sys.exit(1)


def data_uri(path, mime):
    return "data:%s;base64,%s" % (mime, base64.b64encode(path.read_bytes()).decode())


def game_running():
    return subprocess.run(["pgrep", "-f", "Contents/MacOS/ultrastardx"],
                          capture_output=True).returncode == 0


# --------------------------------------------------------------------------
# rendering (headless Chromium, transparent background, exact pixel size)
# --------------------------------------------------------------------------

class Renderer:
    def __init__(self, chrome, fonts_css, tmp):
        self.chrome = chrome
        self.fonts_css = fonts_css
        self.tmp = tmp

    def render(self, out, w, h, css, body):
        html = ("<!doctype html><html><head><meta charset='utf-8'><style>%s"
                "html,body{margin:0;width:%dpx;height:%dpx;overflow:hidden;background:transparent;"
                "font-family:B,Helvetica,Arial,sans-serif;position:relative}*{box-sizing:border-box}%s</style></head>"
                "<body>%s</body></html>") % (self.fonts_css, w, h, css, body)
        src = self.tmp / (out.stem + ".html")
        src.write_text(html, encoding="utf-8")
        # headless Chromium paints only window height minus ~90 px (window
        # chrome), so render a taller window and crop to the requested size
        raw = self.tmp / (out.stem + ".raw.png")
        cmd = [self.chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
               "--default-background-color=00000000", "--force-device-scale-factor=1",
               "--window-size=%d,%d" % (w, h + 160), "--screenshot=" + str(raw), src.as_uri()]
        # headless Chromium occasionally hangs on start: time out and retry
        for attempt in range(3):
            try:
                subprocess.run(cmd, check=True, capture_output=True, timeout=60)
                break
            except subprocess.TimeoutExpired:
                print("  %s: Chromium nie odpowiada, ponawiam" % out.name)
        else:
            die("Chromium nie wyrenderował " + out.name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(raw),
                        "-vf", "crop=%d:%d:0:0" % (w, h), "-pix_fmt", "rgba", str(out)],
                       check=True, capture_output=True)
        info = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(out)],
                              capture_output=True, text=True).stdout
        size = tuple(int(v) for v in re.findall(r"pixel(?:Width|Height): (\d+)", info))
        if size != (w, h):
            die("render %s: %s zamiast %dx%d" % (out.name, size, w, h))
        print("  %-20s %dx%d" % (out.name, w, h))


def qr_svg(seed=11, n=25):
    """placeholder QR code (finder patterns + random modules), not scannable"""
    random.seed(seed)

    def finder(x, y):
        for x0, y0 in ((0, 0), (n - 7, 0), (0, n - 7)):
            if x0 <= x < x0 + 7 and y0 <= y < y0 + 7:
                return max(abs(x - x0 - 3), abs(y - y0 - 3)) != 2
        if (x < 8 and y < 8) or (x >= n - 8 and y < 8) or (x < 8 and y >= n - 8):
            return False
        return None

    cells = []
    for y in range(n):
        for x in range(n):
            f = finder(x, y)
            if f is True or (f is None and random.random() < .5):
                cells.append('<rect x="%d" y="%d" width="1" height="1"/>' % (x, y))
    return ('<svg viewBox="-1 -1 %d %d" shape-rendering="crispEdges">'
            '<rect x="-1" y="-1" width="%d" height="%d" fill="#fff"/>'
            '<g fill="#0d0d0f">%s</g></svg>') % (n + 2, n + 2, n + 2, n + 2, "".join(cells))


def render_art(r, out, vinyl_uri):
    out.mkdir(parents=True, exist_ok=True)

    # waiting screen: designed on a 1600x900 stage, scaled 1.2x to 1920x1080
    r.render(out / "waiting.png", 1920, 1080, """
      body{background:%(bg)s}
      .stage0{position:absolute;left:160px;top:0;width:1600px;height:900px;transform:scale(1.2);transform-origin:50%% 0}
      .halo{position:absolute;left:50%%;top:380px;width:1600px;height:1564px;transform:translate(-50%%,-50%%);
        background:url(%(vin)s) center/contain no-repeat;opacity:.10}
      .stage{position:absolute;left:50%%;top:372px;width:0;height:0}
      .card{position:absolute;width:440px;height:440px;border-radius:18px;left:-220px;top:-220px}
      .year{transform:translate(214px,6px) rotate(7deg);background:linear-gradient(160deg,#ff5f93 0%%,#ff7a6e 55%%,#ff9152 100%%);color:#16100f;
        box-shadow:0 2px 0 rgba(255,255,255,.25) inset,0 -3px 0 rgba(0,0,0,.12) inset,0 30px 60px rgba(0,0,0,.45)}
      .year .in{position:absolute;inset:0;display:flex;flex-direction:column;justify-content:space-between;align-items:center;text-align:center;padding:52px 46px 48px}
      .year .a{font-weight:700;font-size:25px}
      .year .y{font-weight:700;font-size:132px;letter-spacing:-4px;line-height:.9}
      .year .t{font-weight:400;font-size:23px;line-height:1.25;max-width:320px}
      .qrc{transform:translate(-214px,-6px) rotate(-6deg);background:#141416;
        box-shadow:0 1px 0 rgba(255,255,255,.08) inset,0 34px 70px rgba(0,0,0,.55);overflow:hidden}
      .qrc .rings{position:absolute;inset:14px;background:url(%(vin)s) center/contain no-repeat;filter:drop-shadow(0 0 3px rgba(255,255,255,.35)) saturate(1.15)}
      .qrc .q{position:absolute;left:50%%;top:50%%;width:154px;height:154px;transform:translate(-50%%,-50%%);border-radius:12px;overflow:hidden;box-shadow:0 0 0 6px #141416}
      .qrc .q svg{width:100%%;height:100%%;display:block}
    """ % {"bg": BG, "vin": vinyl_uri}, """
      <div class="stage0"><div class="halo"></div><div class="stage">
      <div class="card year"><div class="in"><div class="a">Rick Astley</div><div class="y">1987</div>
        <div class="t">Never Gonna<br>Give You Up</div></div></div>
      <div class="card qrc"><div class="rings"></div><div class="q">%s</div></div>
      </div></div>""" % qr_svg())

    r.render(out / "arrow.png", 72, 46, "", """<svg width="72" height="46" viewBox="0 0 72 46">
      <path d="M4 4 H68 L36 42 Z" fill="%s" stroke="%s" stroke-width="6" stroke-linejoin="round"/></svg>""" % (YELLOW, YELLOW))

    # score screen background: deep brand blue with a soft light from above
    r.render(out / "score_bg.png", 1920, 1080, """
      body{background:%s}
      .g{position:absolute;inset:0;background:radial-gradient(ellipse 70%% 55%% at 50%% 30%%,rgba(0,170,213,.13),rgba(0,170,213,0) 70%%)}
      .v{position:absolute;inset:0;background:radial-gradient(ellipse 85%% 80%% at 50%% 50%%,rgba(0,0,0,0) 55%%,rgba(0,0,0,.45))}
    """ % BG, '<div class="g"></div><div class="v"></div>')

    # white pills, tinted by the game (duel bar). Rendered at 2x for crisp edges.
    for name, w, h in (("pill_sing.png", 3552, 128), ("pill_score.png", 3360, 360)):
        r.render(out / name, w, h, "div{width:100%%;height:100%%;border-radius:%dpx;background:#fff}" % (h // 2), "<div></div>")

    # player tags next to the note lanes
    for name, label, col in (("tag_p1.png", "P1", P1), ("tag_p2.png", "P2", P2)):
        r.render(out / name, 128, 80, """
          div{position:absolute;inset:0;border-radius:14px;background:%s;color:#fff;font-weight:700;font-size:46px;
              letter-spacing:.08em;display:flex;align-items:center;justify-content:center;padding:6px 0 0 6px}
        """ % col, "<div>%s</div>" % label)

    # year cards (score screen, square) and mini cards (sing screen), with shadow padding
    for cname, (c1, c2, c3) in CARDS.items():
        grad = "linear-gradient(160deg,%s 0%%,%s 55%%,%s 100%%)" % (c1, c2, c3)
        # square: card 512px + 64px shadow pad on each side
        r.render(out / ("card_%s.png" % cname), 640, 640, """
          div{position:absolute;left:64px;top:64px;width:512px;height:512px;border-radius:28px;background:%s;
              box-shadow:0 3px 0 rgba(255,255,255,.28) inset,0 -5px 0 rgba(0,0,0,.12) inset,0 28px 44px rgba(0,0,0,.45)}
        """ % grad, "<div></div>")
        # mini: card 1200x192 (600x96 on screen at 2x) + 48px pad
        r.render(out / ("mini_%s.png" % cname), 1296, 288, """
          div{position:absolute;left:48px;top:48px;width:1200px;height:192px;border-radius:26px;background:%s;
              box-shadow:0 3px 0 rgba(255,255,255,.28) inset,0 -4px 0 rgba(0,0,0,.12) inset,0 18px 32px rgba(0,0,0,.45)}
        """ % grad, "<div></div>")


def render_skin_art(r, skin_dir):
    # darkens the video where notes and lyrics are, keeps the picture alive between
    r.render(skin_dir / "hitster_scrim.png", 1920, 1080, """
      div{position:absolute;inset:0;background:linear-gradient(180deg,
        rgba(0,22,29,.86) 0,rgba(0,22,29,.25) 20%,rgba(0,22,29,.62) 32%,
        rgba(0,22,29,.62) 78%,rgba(0,22,29,.93) 86%,rgba(0,22,29,.93) 100%)}
    """, "<div></div>")
    r.render(skin_dir / "hitster_empty.png", 4, 4, "", "")

    # menus (operator only): brand background without the USDX logo,
    # dark bottom bar instead of the white legend strip
    r.render(skin_dir / "hitster_menu_bg.png", 1920, 1080, """
      body{background:%s}
      .g{position:absolute;inset:0;background:radial-gradient(ellipse 75%% 60%% at 50%% 35%%,rgba(0,170,213,.16),rgba(0,170,213,0) 70%%)}
      .v{position:absolute;inset:0;background:radial-gradient(ellipse 90%% 85%% at 50%% 50%%,rgba(0,0,0,0) 55%%,rgba(0,0,0,.5))}
    """ % BG, '<div class="g"></div><div class="v"></div>')
    r.render(skin_dir / "hitster_bar.png", 550, 30, "div{position:absolute;inset:0;background:rgba(0,10,14,.78)}", "<div></div>")

    # notes: the game replaces only the hue with the player colour (saturation
    # and brightness stay), so they are drawn in saturated red here.
    #   notesPlain* = notes still to sing: 30% fill, solid edge
    #   notes*      = what the player sang: solid
    #   notesBg*    = soft pulsing glow behind the sung notes
    pills = {
        "notesPlain": "background:rgba(230,0,0,.30);border:4px solid rgb(230,0,0);"
                      "box-shadow:0 0 0 3px rgba(40,0,0,.55)",
        "notes": "background:linear-gradient(180deg,rgb(255,105,105) 0,rgb(232,0,0) 42%,rgb(205,0,0) 100%);"
                 "box-shadow:0 0 0 3px rgba(40,0,0,.55)",
        "notesBg": "background:rgba(230,0,0,.55);filter:blur(4px)",
    }
    for name, style in pills.items():
        full = r.tmp / (name + "_full.png")
        r.render(full, 256, 64, "div{position:absolute;left:6px;right:6px;top:6px;bottom:6px;border-radius:26px;%s}"
                 % style, "<div></div>")
        for part, x, w in (("Left", 0, 32), ("Mid", 96, 64), ("Right", 224, 32)):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(full), "-vf",
                            "crop=%d:64:%d:0" % (w, x), "-pix_fmt", "rgba",
                            str(skin_dir / ("[sing]%s%s.png" % (name, part)))], check=True)


# --------------------------------------------------------------------------
# theme
# --------------------------------------------------------------------------

def ini_set(lines, section, key, value):
    """sets key in [section] of an ini kept as a list of lines (adds both if missing)"""
    head = re.compile(r"^\s*\[(.+?)\]")
    start = None
    for i, line in enumerate(lines):
        m = head.match(line)
        if m and m.group(1).strip() == section:
            start = i
            break
    if start is None:
        lines += ["", "[%s]" % section, "%s = %s" % (key, value)]
        return
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if head.match(lines[i]):
            end = i
            break
    kre = re.compile(r"^\s*%s\s*=" % re.escape(key), re.IGNORECASE)
    for i in range(start + 1, end):
        if kre.match(lines[i]):
            lines[i] = "%s = %s" % (key, value)
            return
    lines.insert(start + 1, "%s = %s" % (key, value))


OFF = "-2000"  # x for elements moved off screen

# Sing screen: the game draws only video, scrim, notes and lyrics; the duel bar,
# song card and player tags are drawn by src/base/UHitsterUI.pas.
THEME_SETTINGS = {
    "Theme": {"Name": "Hitster", "Creator": "Ebert Studio for TM Toys", "DefaultSkin": "Hitster"},
    "SingStatic1": {"Tex": "HitsterScrim", "X": "0", "Y": "0", "W": "800", "H": "600",
                    "Z": "0", "Color": "White", "Type": "Transparent"},
    "SingLyricsBar": {"Tex": "HitsterEmpty"},
    "SingLyricsBarDuet": {"Tex": "HitsterEmpty"},
    "SingTimeBar": {"X": OFF},
    "SingTimeProgress": {"X": OFF},
    "SingTimeText": {"X": OFF},
    "SingTimeLabelText": {"X": OFF},
    "SingSongNameStatic": {"X": OFF},
    "SingSongNameText": {"X": OFF},
    "SingInfoMessageBG": {"X": OFF},
    "SingInfoMessageText": {"X": OFF},
}
for p in ("SingP1", "SingP1TwoP", "SingP2R", "SingP1ThreeP", "SingP2M", "SingP3R"):
    for part in ("Static", "Text", "Static2", "TextScore", "SingBar", "Avatar"):
        THEME_SETTINGS.setdefault(p + part, {})["X"] = OFF


# skin colour "Blue" drives ColorDark / ColorLight / ColorLightest in the base
# theme; the game appends those after [Colors], so they cannot be redefined.
# The Hitster theme uses its own names instead.
THEME_COLORS = {
    "HitDark": "14 52 64",        # buttons and rows, not selected
    "HitLight": "230 0 126",      # selected: Hitster pink (= P1)
    "HitLightest": "147 172 179", # secondary text
}

MENU_SETTINGS = {
    "DefaultBar1": {"Color": "HitDark", "Type": "Transparent", "Tex": "Leiste2"},
    "DefaultLegendText": {"Color": "White"},
    "DefaultSelectSlide": {"STDColor": "HitLightest"},
}


def build_theme(game, userdir, r):
    src_ini = game / "themes/Modern.ini"
    src_skin = game / "themes/Modern"
    if not src_ini.is_file() or not (src_skin / "Blue.ini").is_file():
        die("nie ma motywu Modern w " + str(game / "themes"))

    themes = userdir / "themes"
    skin_dir = themes / "Hitster"
    if skin_dir.exists():
        shutil.rmtree(skin_dir)
    shutil.copytree(src_skin, skin_dir, ignore=shutil.ignore_patterns("*.ini"))

    text = src_ini.read_text(encoding="utf-8", errors="replace")
    for old, new in (("ColorLightest", "HitLightest"), ("ColorLight", "HitLight"), ("ColorDark", "HitDark")):
        text = re.sub(r"\b%s\b" % old, new, text)
    # no mirror reflections under buttons and bars
    text = re.sub(r"(?m)^(\s*Reflection\s*=\s*)1\s*$", r"\g<1>0", text)
    lines = text.splitlines()
    for k, v in THEME_COLORS.items():
        ini_set(lines, "Colors", k, v)
    for section, keys in MENU_SETTINGS.items():
        for k, v in keys.items():
            ini_set(lines, section, k, v)
    for section, keys in THEME_SETTINGS.items():
        present = any(re.match(r"^\s*\[%s\]" % re.escape(section), l) for l in lines)
        if not present and section.startswith("Sing") and section != "SingStatic1":
            continue  # do not invent sections the base theme does not have
        for k, v in keys.items():
            ini_set(lines, section, k, v)
    (themes / "Hitster.ini").write_text("\n".join(lines) + "\n", encoding="utf-8")

    skin = (src_skin / "Blue.ini").read_text(encoding="utf-8", errors="replace").splitlines()
    ini_set(skin, "Skin", "Theme", "Hitster")
    ini_set(skin, "Skin", "Name", "Hitster")
    ini_set(skin, "Textures", "HitsterScrim", "hitster_scrim.png")
    ini_set(skin, "Textures", "HitsterEmpty", "hitster_empty.png")
    # backgrounds (the Modern ones carry the USDX logo) and the legend bar
    for key in ("LoadingBG", "MainBG", "SongBG", "ScoreScreenBG", "HighScoresBG", "OptionsBG",
                "EditBG", "EditSubBG", "PartyBG"):
        ini_set(skin, "Textures", key, "hitster_menu_bg.png")
    ini_set(skin, "Textures", "Leiste2", "hitster_bar.png")
    # USDX pictograms next to the menu titles
    for key in ("MainIcon", "IconOption", "IconEdit", "IconSongMenu", "ScoreIcon", "PartyIcon",
                "StatIcon", "VideoIcon", "SongCD", "MainSearch"):
        ini_set(skin, "Textures", key, "hitster_empty.png")
    (skin_dir / "Hitster.ini").write_text("\n".join(skin) + "\n", encoding="utf-8")

    render_skin_art(r, skin_dir)
    print("  motyw: " + str(themes / "Hitster.ini"))


# --------------------------------------------------------------------------
# fonts
# --------------------------------------------------------------------------

FONTS_INI = """; Hitster Karaoke: generated by hitster/install.py
; family 0 = GUI font and lyrics, family 1 = light text
[Font_Brown]
Name=Brown
RegularFile=BrownStd-Regular.otf
RegularMaxResolution=128
RegularStretch=0.75
RegularFallbackFile1=NotoSans/GoNotoKurrent-Regular.ttf
RegularFallbackFile2=NotoSans/GoNotoCurrent-Regular.ttf
BoldFile=BrownStd-Bold.otf
BoldMaxResolution=128
BoldStretch=0.75
BoldFallbackFile1=NotoSans/GoNotoKurrent-Bold.ttf
BoldFallbackFile2=NotoSans/GoNotoCurrent-Bold.ttf
OutlineFile=BrownStd-Bold.otf
OutlineOutline=0.06
OutlineMaxResolution=128
OutlineStretch=0.75
OutlineFallbackFile1=NotoSans/GoNotoKurrent-Bold.ttf
OutlineFallbackFile2=NotoSans/GoNotoCurrent-Bold.ttf
BoldHighResFile=BrownStd-Bold.otf
BoldHighResMaxResolution=256
BoldHighResPreCache=0
BoldHighResStretch=0.75
BoldHighResFallbackFile1=NotoSans/GoNotoKurrent-Bold.ttf

[Font_BrownLight]
Name=Brown Light
RegularFile=BrownStd-Light.otf
RegularMaxResolution=128
RegularStretch=0.75
RegularFallbackFile1=NotoSans/GoNotoKurrent-Regular.ttf
BoldFile=BrownStd-Regular.otf
BoldMaxResolution=128
BoldStretch=0.75
BoldFallbackFile1=NotoSans/GoNotoKurrent-Regular.ttf
OutlineFile=BrownStd-Regular.otf
OutlineOutline=0.06
OutlineStretch=0.75
BoldHighResFile=BrownStd-Regular.otf
BoldHighResMaxResolution=256
BoldHighResPreCache=0
BoldHighResStretch=0.75

[Font_NotoSans]
Name=Noto Sans
RegularFile=NotoSans/GoNotoKurrent-Regular.ttf
RegularFallbackFile1=NotoSans/GoNotoCurrent-Regular.ttf
BoldFile=NotoSans/GoNotoKurrent-Bold.ttf
BoldFallbackFile1=NotoSans/GoNotoCurrent-Bold.ttf
OutlineFile=NotoSans/GoNotoKurrent-Bold.ttf
OutlineOutline=0.06
OutlineFallbackFile1=NotoSans/GoNotoCurrent-Bold.ttf
BoldHighResFile=NotoSans/GoNotoKurrent-Bold.ttf
BoldHighResMaxResolution=256
BoldHighResPreCache=0
BoldHighResOutline=0.02
"""


def install_fonts(game, brand, userdir):
    fonts = userdir / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    for n in ("Light", "Regular", "Bold"):
        src = brand / "drive" / ("BrownStd-%s.otf" % n)
        if not src.is_file():
            die("brak czcionki " + str(src))
        shutil.copy2(src, fonts / src.name)
    noto = game / "fonts/NotoSans"
    if not noto.is_dir():
        die("brak " + str(noto))
    shutil.copytree(noto, fonts / "NotoSans", dirs_exist_ok=True)
    (fonts / "fonts.ini").write_text(FONTS_INI, encoding="utf-8")
    print("  czcionki: " + str(fonts / "fonts.ini"))


# --------------------------------------------------------------------------
# config.ini
# --------------------------------------------------------------------------

CONFIG = {
    "Themes": {"Theme": "Hitster", "Skin": "Hitster", "Color": "Blue"},
    "Lyrics": {"LyricsFont": "Brown", "LyricsStyle": "Outline", "LyricsEffect": "Slide", "NoteLines": "Off"},
    # sung text yellow, current line white, next line dimmed (60% white on the scrim)
    "Jukebox": {"LyricsFont": "Brown", "LyricsStyle": "Outline",
                "SingLineColor": "FFCE44", "ActualLineColor": "FFFFFF", "NextLineColor": "9AA6AA",
                "SingLineOColor": "00161D", "ActualLineOColor": "00161D", "NextLineOColor": "00161D"},
    # P1 pink (6), P2 cyan (12): exact Hitster values live in UThemes.GetPlayerColor
    "PlayerColor": {"P1": "6", "P2": "12"},
    "Advanced": {"ScreenFade": "Off", "SingScores": "Off", "LoadAnimation": "Off", "EffectSing": "On"},
    "Graphics": {"VideoPreview": "Off", "MovieSize": "Full [BG+Vid]", "Oscilloscope": "Off"},
    "Sound": {"PreviewVolume": "0", "BackgroundMusic": "Off"},
    "Game": {"Language": "English", "Tabs": "Off"},
    "Remote": {"Design": "1", "TeaserVolume": "70", "Aspect": "16:9",
               "WaitingText": "Scan your card", "WaitingSubText": "and sing the duel"},
}


def patch_config(userdir, locations):
    path = userdir / "config.ini"
    if not path.is_file():
        print("  config.ini nie istnieje, uruchom grę raz i powtórz bez --no-config")
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for section, keys in list(CONFIG.items()) + list(locations.items()):
        for k, v in keys.items():
            ini_set(lines, section, k, v)
    shutil.copy2(path, path.with_suffix(".ini.bak-hitster"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("  config.ini zaktualizowany (kopia: config.ini.bak-hitster)")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brand", required=True, type=pathlib.Path,
                    help="katalog materiałów marki (zawiera drive/BrownStd-*.otf i drive/Vinyl circles.ai)")
    ap.add_argument("--userdir", type=pathlib.Path, default=DEFAULT_USERDIR)
    ap.add_argument("--game", type=pathlib.Path, default=REPO / "game", help="katalog danych gry (themes, fonts)")
    ap.add_argument("--data", type=pathlib.Path,
                    help="katalog na dane (np. dysk zewnętrzny): grafiki w DATA/art, piosenki w DATA/songs")
    ap.add_argument("--songs", type=pathlib.Path, help="katalog biblioteki piosenek (nadpisuje DATA/songs)")
    ap.add_argument("--no-config", action="store_true", help="nie zmieniaj config.ini")
    args = ap.parse_args()

    brand = args.brand.expanduser().resolve()
    userdir = args.userdir.expanduser()

    data = args.data.expanduser().resolve() if args.data else None
    songs = args.songs.expanduser().resolve() if args.songs else (data / "songs" if data else None)
    art_dir = data / "art" if data else userdir / "hitster"
    for d in (data, songs):
        if d is None:
            continue
        parts = d.parts
        # /Volumes/<name>/...: the drive has to be connected
        if len(parts) > 2 and parts[1] == "Volumes" and not pathlib.Path("/Volumes", parts[2]).is_dir():
            die("dysk /Volumes/%s nie jest podłączony" % parts[2])
    if songs is not None:
        songs.mkdir(parents=True, exist_ok=True)
    # ArtDir is written explicitly, so running without --data moves art back home
    locations = {"Remote": {"ArtDir": str(art_dir) if data else ""}}
    if songs is not None:
        locations["Directories"] = {"SongDir1": str(songs)}
    if not args.no_config and game_running():
        die("gra jest uruchomiona; zamknij ją (nadpisałaby config.ini)")
    chrome = next((c for c in CHROMES if os.path.exists(c)), None)
    if chrome is None:
        die("potrzebny Chromium albo Google Chrome")
    vinyl_src = brand / "drive/Vinyl circles.ai"
    if not vinyl_src.is_file():
        die("brak " + str(vinyl_src))

    with tempfile.TemporaryDirectory() as tmpd:
        tmp = pathlib.Path(tmpd)
        # the .ai file is PDF compatible; sips rasterizes it with transparency
        shutil.copy2(vinyl_src, tmp / "vinyl.pdf")
        subprocess.run(["sips", "-s", "format", "png", "-Z", "1800", str(tmp / "vinyl.pdf"),
                        "--out", str(tmp / "vinyl.png")], check=True, capture_output=True)
        fonts_css = "".join(
            "@font-face{font-family:B;src:url(%s);font-weight:%d}"
            % (data_uri(brand / "drive" / ("BrownStd-%s.otf" % n), "font/otf"), w)
            for n, w in (("Light", 300), ("Regular", 400), ("Bold", 700)))
        r = Renderer(chrome, fonts_css, tmp)

        print("czcionki")
        install_fonts(args.game, brand, userdir)
        print("motyw")
        build_theme(args.game, userdir, r)
        print("grafiki")
        render_art(r, art_dir, data_uri(tmp / "vinyl.png", "image/png"))
        print("  grafiki: " + str(art_dir))
        if songs is not None:
            print("  piosenki: " + str(songs) + "  (PLAY hitster/<folder> = " + str(songs / "hitster") + "/<folder>)")
        if not args.no_config:
            print("config.ini")
            patch_config(userdir, locations)
        elif data or songs:
            print("  --no-config: wpisz ręcznie do config.ini: " + repr(locations))
    print("gotowe")


if __name__ == "__main__":
    main()
