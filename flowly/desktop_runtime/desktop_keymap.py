"""Layout characters for the virtual desktop's keymap, placed as the application UID.

The viewer sends keysyms, not the client's physical keys. Xvnc adds a keysym
it cannot find to a spare keycode at the moment it is typed, and an X client
such as Chromium interprets that key event with its previous keymap: a new
character was dropped or became the next new one (ş typed as ğ). Placing the
common layout characters in the keymap before any client starts makes them
ordinary keys. Any other character keysym is mapped by ``RemoteKeymap`` before
the input broker forwards it, and the clients get time to load the new keymap.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable

# Lower/upper pairs, four per key (Shift, ISO_Level3 and both).
KEYMAP_KEYSYMS = (
    # Turkish letters outside Latin-1: ş Ş ğ Ğ ı İ.
    0x1BA, 0x1AA, 0x2BB, 0x2AB, 0x2B9, 0x2A9,
    # Latin-1 letters à–þ with their capitals, ß and ÿ.
    *(value for lower in range(0xE0, 0xFF) if lower != 0xF7 for value in (lower, lower - 0x20)), 0xDF, 0xFF,
    # € £ ¥ § ° « » – — ‘ ’ “ ” … ¿ ¡
    0x20AC, 0xA3, 0xA5, 0xA7, 0xB0, 0xAB, 0xBB, 0xAAA, 0xAA9, 0xAD0, 0xAD1, 0xAD2, 0xAD3, 0xAAE, 0xBF, 0xA1,
)
# Keycodes left free after seeding: EXTRA_SLOTS for RemoteKeymap, the rest
# for Xvnc's own additions.
SPARE_KEYCODES = 12
EXTRA_SLOTS = 8


def needs_mapping(keysym: int) -> bool:
    """A character keysym that is neither ASCII nor seeded."""
    if 0x20 <= keysym <= 0x7E or keysym in _SEEDED:
        return False
    return 0xA0 <= keysym < 0xFD00 or 0x01000000 < keysym <= 0x0110FFFF


_SEEDED = frozenset(KEYMAP_KEYSYMS)


class RemoteKeymap:
    """Maps character keysyms outside the seeded set before they are typed.

    ``upload`` rebuilds the keymap with the given extra keysyms (at most
    EXTRA_SLOTS, least recently used evicted). A failed upload leaves the
    keysym to Xvnc's own addition, which a client may misread.
    """

    SETTLE_SECONDS = 0.15

    def __init__(self, upload: Callable[[list[int]], bool]):
        self._upload = upload
        self._slots: list[int] = []  # least recently used first
        self._lock = threading.Lock()

    def prepare(self, keysyms: Iterable[int]) -> None:
        needed = list(dict.fromkeys(keysym for keysym in keysyms if needs_mapping(keysym)))[:EXTRA_SLOTS]
        if not needed:
            return
        with self._lock:
            missing = [keysym for keysym in needed if keysym not in self._slots]
            kept = [keysym for keysym in self._slots if keysym not in needed]
            if not missing:
                self._slots = kept + needed
                return
            kept = kept[len(kept) + len(needed) - EXTRA_SLOTS:] if len(kept) + len(needed) > EXTRA_SLOTS else kept
            proposed = kept + needed
            if self._upload(proposed):
                self._slots = proposed
                # X clients load the new keymap asynchronously.
                time.sleep(self.SETTLE_SECONDS)


KEYMAP_SOURCE = ("KEYMAP_KEYSYMS = " + repr(KEYMAP_KEYSYMS) + "\nSPARE_KEYCODES = " + repr(SPARE_KEYCODES)
                 + "\nEXTRA_SLOTS = " + repr(EXTRA_SLOTS) + r"""
import os
import re
import subprocess
import sys
import tempfile

if os.geteuid() == 0:
    raise SystemExit("keymap seeding requires the application account")
xkbcomp = sys.argv[1]
if not os.path.isabs(xkbcomp):
    raise SystemExit("invalid xkbcomp path")
extras = [int(value, 16) for value in sys.argv[2:]]
if len(extras) > EXTRA_SLOTS or len(set(extras)) != len(extras) or not all(
        0xA0 <= value < 0xFD00 or 0x01000000 < value <= 0x0110FFFF for value in extras):
    raise SystemExit("invalid extra keysyms")
display = os.environ["DISPLAY"]
base = os.path.join(os.environ["XDG_RUNTIME_DIR"], "keymap-base.xkb")

def run(arguments):
    subprocess.run([xkbcomp, "-w", "0", *arguments], check=True, timeout=10,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

with tempfile.TemporaryDirectory(dir=os.environ["XDG_RUNTIME_DIR"]) as work:
    if not extras:
        # Seeding runs once on the fresh server keymap; later calls rebuild
        # from it so repeated extras never accumulate or duplicate keys.
        current = os.path.join(work, "current.xkb")
        run(["-xkb", display, current])
        os.replace(current, base)
    with open(base) as source:
        keymap = source.read(1024 * 1024)

    def section(name, after):
        begin = keymap.index(name)
        stop = min([keymap.find(other, begin) for other in after if keymap.find(other, begin) > 0]
                   or [keymap.rindex("};")])
        return begin, begin + keymap[begin:stop].rindex("};")

    keycodes_at, keycodes_end = section("xkb_keycodes", ("xkb_types",))
    symbols_at, symbols_end = section("xkb_symbols", ("xkb_geometry",))
    keycodes, symbols = keymap[keycodes_at:keycodes_end], keymap[symbols_at:symbols_end]
    names = re.findall(r"<([A-Za-z0-9+_-]{1,4})>\s*=\s*(\d+)\s*;", keycodes)
    code_of = {name: int(code) for name, code in names}
    definitions = {match.group(1): match for match in re.finditer(
        r"\bkey\s+<([A-Za-z0-9+_-]{1,4})>\s*\{[^{}]*\};", symbols)}
    used = {code_of[name] for name in re.findall(r"\bkey\s+<([A-Za-z0-9+_-]{1,4})>", symbols) if name in code_of}
    maximum = int(re.search(r"maximum\s*=\s*(\d+)\s*;", keycodes).group(1))
    # 8 and 9 are never touched. Keys that only carry XF86 media keysyms are
    # reused first: the viewer sends keysyms, not a keyboard's physical keys,
    # and Xvnc re-adds such a keysym if one is ever sent.
    free = [code for code in range(min(maximum, 255), 9, -1) if code not in used]
    mapped = " ".join(re.findall(r"modifier_map[^;]*;", symbols))
    reusable = sorted((code_of[name] for name, match in definitions.items()
                       if name in code_of and "<%s>" % name not in mapped
                       and re.search(r"XF86", match.group(0))
                       and all(keysym.startswith("XF86") for keysym in re.findall(
                           r"[A-Za-z_][A-Za-z0-9_]*", " ".join(re.findall(r"\[([^\]]*)\]", match.group(0))))
                               if keysym not in ("NoSymbol", "Group1"))), reverse=True)
    groups = [["0x%x" % value for value in KEYMAP_KEYSYMS[index:index + 4]]
              for index in range(0, len(KEYMAP_KEYSYMS), 4)]
    targets = (reusable + free)[:len(groups)]
    remaining = [code for code in free if code not in targets]
    if len(targets) < len(groups) or len(remaining) < SPARE_KEYCODES:
        raise SystemExit("not enough spare keycodes: %d free, %d reusable, %d keys"
                         % (len(free), len(reusable), len(groups)))
    named = {}
    for name, code in names:
        named.setdefault(int(code), name)
    new_names, lines, replaced = [], [], set()

    def place(code, keysyms, kind):
        name = named.get(code)
        if name is None:
            name = "Q%03d" % code
            if name in code_of:
                raise SystemExit("keycode name collision")
            new_names.append("    <%s> = %d;" % (name, code))
        elif code in used:
            replaced.add(name)
        lines.append('    key <%s> { type= "%s", symbols[Group1]= [ %s ] };' % (name, kind, ", ".join(keysyms)))

    for code, group in zip(targets, groups):
        place(code, group + ["NoSymbol"] * (4 - len(group)), "FOUR_LEVEL")
    for code, value in zip(remaining, extras):
        place(code, ["0x%x" % value], "ONE_LEVEL")
    kept = symbols
    for name in replaced:
        kept = kept.replace(definitions[name].group(0), "")
    seeded = (keymap[:keycodes_end] + "".join(line + "\n" for line in new_names) + keymap[keycodes_end:symbols_at]
              + kept + "".join(line + "\n" for line in lines) + keymap[symbols_end:])
    target = os.path.join(work, "seeded.xkb")
    with open(target, "w") as output:
        output.write(seeded)
    run([target, display])
""")
