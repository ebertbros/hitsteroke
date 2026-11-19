#!/usr/bin/env python3
"""Remote control client and test tool for the UltraStar Deluxe "pilot".

Protocol: one UTF-8 command per UDP datagram to 127.0.0.1:<port>,
one JSON line back. See remote/README.md.

Usage:
  usdx_remote.py play "Artist - Title"     start a song (folder relative to a song dir)
  usdx_remote.py stop                      stop the song, back to the waiting screen
  usdx_remote.py status                    print the game state
  usdx_remote.py wait-playing              block until a song is playing
  usdx_remote.py stress 20 [song ...]      N x PLAY/STOP cycles with timing + memory
  usdx_remote.py switch 10 [song ...]      N x PLAY while another song is playing

Options: --port N (default 47700), --timeout S (reply timeout, default 1.0)
"""

import argparse
import json
import socket
import subprocess
import sys
import time

DEFAULT_PORT = 47700


class WaitTimeout(Exception):
    """The game answered, but did not reach the expected state in time."""


class Remote:
    def __init__(self, port=DEFAULT_PORT, timeout=1.0):
        self.addr = ("127.0.0.1", port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)

    def send(self, command):
        """Send one command, return the parsed JSON reply (raises on timeout)."""
        self.sock.sendto(command.encode("utf-8"), self.addr)
        data, _ = self.sock.recvfrom(65535)
        return json.loads(data.decode("utf-8"))

    def play(self, song):
        return self.send("PLAY " + song)

    def stop(self):
        return self.send("STOP")

    def status(self):
        return self.send("STATUS")

    def wait_for(self, predicate, timeout=15.0, interval=0.05):
        """Poll STATUS until predicate(status) is true; returns (status, seconds)."""
        t0 = time.monotonic()
        while True:
            st = self.status()
            if predicate(st):
                return st, time.monotonic() - t0
            if time.monotonic() - t0 > timeout:
                raise WaitTimeout("timeout, last status: " + json.dumps(st, ensure_ascii=False))
            time.sleep(interval)


def is_playing(st):
    return st.get("state") == "playing" and st.get("screen") == "sing"


def is_idle(st):
    return st.get("state") == "idle" and st.get("screen") == "song"


def game_rss_mb():
    """Resident memory of the running game in MB (None if not found)."""
    try:
        pids = subprocess.run(["pgrep", "-f", "Contents/MacOS/ultrastardx"],
                              capture_output=True, text=True).stdout.split()
        if not pids:
            return None
        rss_kb = subprocess.run(["ps", "-o", "rss=", "-p", pids[0]],
                                capture_output=True, text=True).stdout.strip()
        return int(rss_kb) / 1024.0
    except (OSError, ValueError):
        return None


def print_json(obj):
    print(json.dumps(obj, ensure_ascii=False))


def cmd_stress(r, cycles, songs, play_seconds):
    if not songs:
        sys.exit("stress: give at least one song folder")
    print(f"stress: {cycles} cycles PLAY -> playing -> STOP -> idle, songs: {songs}")
    rss0 = game_rss_mb()
    times = []
    for i in range(cycles):
        song = songs[i % len(songs)]
        rep = r.play(song)
        if not rep.get("ok"):
            sys.exit(f"[{i+1}] PLAY failed: {rep}")
        st, dt = r.wait_for(is_playing)
        times.append(dt)
        time.sleep(play_seconds)
        r.stop()
        r.wait_for(is_idle)
        rss = game_rss_mb()
        print(f"[{i+1:2d}/{cycles}] {song!r}: playing after {dt*1000:.0f} ms "
              f"(game: {st.get('last_start_ms')} ms), RSS {rss:.0f} MB" if rss else
              f"[{i+1:2d}/{cycles}] {song!r}: playing after {dt*1000:.0f} ms")
    rss1 = game_rss_mb()
    print(f"start time: min {min(times)*1000:.0f} ms, avg {sum(times)/len(times)*1000:.0f} ms, "
          f"max {max(times)*1000:.0f} ms")
    if rss0 and rss1:
        print(f"RSS: {rss0:.0f} MB -> {rss1:.0f} MB ({rss1-rss0:+.0f} MB)")
    over = [t for t in times if t > 2.0]
    print("RESULT:", "OK" if not over else f"{len(over)} starts over 2 s")


def cmd_switch(r, cycles, songs, play_seconds):
    if len(songs) < 2:
        sys.exit("switch: give at least two song folders")
    print(f"switch: {cycles} x PLAY while another song is playing")
    r.play(songs[0])
    r.wait_for(is_playing)
    times = []
    for i in range(cycles):
        song = songs[(i + 1) % len(songs)]
        time.sleep(play_seconds)
        starts = r.status().get("starts", 0)
        rep = r.play(song)
        if not rep.get("ok"):
            sys.exit(f"[{i+1}] PLAY failed: {rep}")
        # wait until a new song start was counted and it is the requested one
        st, dt = r.wait_for(lambda s: is_playing(s) and s.get("starts", 0) > starts
                            and s.get("song") == rep.get("song"))
        times.append(dt)
        print(f"[{i+1:2d}/{cycles}] -> {song!r}: playing after {dt*1000:.0f} ms "
              f"(game: {st.get('last_start_ms')} ms)")
    r.stop()
    print(f"switch time: min {min(times)*1000:.0f} ms, avg {sum(times)/len(times)*1000:.0f} ms, "
          f"max {max(times)*1000:.0f} ms")
    over = [t for t in times if t > 2.0]
    print("RESULT:", "OK" if not over else f"{len(over)} switches over 2 s")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=1.0)
    p.add_argument("--play-seconds", type=float, default=2.0,
                   help="how long each song plays in stress/switch (default 2)")
    p.add_argument("command", choices=["play", "stop", "status", "wait-playing", "stress", "switch", "raw"])
    p.add_argument("args", nargs="*")
    a = p.parse_args()

    r = Remote(a.port, a.timeout)
    try:
        if a.command == "play":
            print_json(r.play(" ".join(a.args)))
        elif a.command == "stop":
            print_json(r.stop())
        elif a.command == "status":
            print_json(r.status())
        elif a.command == "raw":
            print_json(r.send(" ".join(a.args)))
        elif a.command == "wait-playing":
            st, dt = r.wait_for(is_playing)
            print_json(st)
        elif a.command in ("stress", "switch"):
            if not a.args:
                sys.exit(f"{a.command}: usage: {a.command} N song [song ...]")
            n = int(a.args[0])
            fn = cmd_stress if a.command == "stress" else cmd_switch
            fn(r, n, a.args[1:], a.play_seconds)
    except WaitTimeout as e:
        sys.exit(str(e))
    except socket.timeout:
        sys.exit(f"no reply from 127.0.0.1:{a.port} - is the game running with [Remote] Enabled=1?")


if __name__ == "__main__":
    main()
