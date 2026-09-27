"""Static layout characters for the virtual desktop's keymap, as the application UID.

The viewer sends keysyms, not the client's physical keys. Xvnc adds a keysym
it cannot find to a spare keycode at the moment it is typed, and an X client
such as Chromium interprets that key event with its previous keymap: the first
new character was dropped or became the next new one (ş typed as ğ). Placing
the common layout characters in the keymap before any client starts makes
them ordinary keys. A few spare keycodes stay free for Xvnc's own additions.
"""

# Lower/upper pairs, four per key (Shift, ISO_Level3 and both).
KEYMAP_KEYSYMS = (
    # Turkish letters outside Latin-1 (ı and İ share a key: Shift maps both ways).
    "scedilla", "Scedilla", "gbreve", "Gbreve", "idotless", "Iabovedot",
    # Latin-1 letters.
    "agrave", "Agrave", "aacute", "Aacute", "acircumflex", "Acircumflex", "atilde", "Atilde",
    "adiaeresis", "Adiaeresis", "aring", "Aring", "ae", "AE", "ccedilla", "Ccedilla",
    "egrave", "Egrave", "eacute", "Eacute", "ecircumflex", "Ecircumflex", "ediaeresis", "Ediaeresis",
    "igrave", "Igrave", "iacute", "Iacute", "icircumflex", "Icircumflex", "idiaeresis", "Idiaeresis",
    "eth", "ETH", "ntilde", "Ntilde", "ograve", "Ograve", "oacute", "Oacute",
    "ocircumflex", "Ocircumflex", "otilde", "Otilde", "odiaeresis", "Odiaeresis", "oslash", "Oslash",
    "ugrave", "Ugrave", "uacute", "Uacute", "ucircumflex", "Ucircumflex", "udiaeresis", "Udiaeresis",
    "yacute", "Yacute", "thorn", "THORN", "ssharp", "ydiaeresis",
    # Common typography and currency.
    "EuroSign", "sterling", "yen", "section", "degree", "guillemotleft", "guillemotright", "endash",
    "emdash", "leftsinglequotemark", "rightsinglequotemark", "leftdoublequotemark",
    "rightdoublequotemark", "ellipsis", "questiondown", "exclamdown",
)
SPARE_KEYCODES = 12

KEYMAP_SOURCE = "KEYMAP_KEYSYMS = " + repr(KEYMAP_KEYSYMS) + "\nSPARE_KEYCODES = " + repr(SPARE_KEYCODES) + r"""
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
display = os.environ["DISPLAY"]
with tempfile.TemporaryDirectory(dir=os.environ["XDG_RUNTIME_DIR"]) as work:
    current = os.path.join(work, "current.xkb")
    subprocess.run([xkbcomp, "-w", "0", "-xkb", display, current], check=True, timeout=10,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with open(current) as source:
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
    definitions = {match.group(1): match for match in re.finditer(r"\bkey\s+<([A-Za-z0-9+_-]{1,4})>\s*\{[^{}]*\};", symbols)}
    used = {code_of[name] for name in re.findall(r"\bkey\s+<([A-Za-z0-9+_-]{1,4})>", symbols) if name in code_of}
    present = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", " ".join(re.findall(r"\[([^\]]*)\]", symbols))))
    maximum = int(re.search(r"maximum\s*=\s*(\d+)\s*;", keycodes).group(1))
    # 8 and 9 are never touched. Keys that only carry XF86 media keysyms are
    # reused first: the viewer sends keysyms, not a keyboard's physical keys,
    # and Xvnc re-adds such a keysym if one is ever sent.
    free = [code for code in range(min(maximum, 255), 9, -1) if code not in used]
    mapped = " ".join(re.findall(r"modifier_map[^;]*;", symbols))
    reusable = sorted((code_of[name] for name, match in definitions.items()
                       if name in code_of and "<%s>" % name not in mapped
                       and all(keysym.startswith("XF86") for keysym in re.findall(
                           r"[A-Za-z_][A-Za-z0-9_]*", " ".join(re.findall(r"\[([^\]]*)\]", match.group(0))))
                               if keysym not in ("NoSymbol", "Group1"))
                       and re.search(r"XF86", match.group(0))), reverse=True)
    wanted = [keysym for keysym in KEYMAP_KEYSYMS if keysym not in present]
    groups = [wanted[index:index + 4] for index in range(0, len(wanted), 4)]
    targets = (reusable + free)[:len(groups)]
    if len(targets) < len(groups) or len([code for code in free if code not in targets]) < SPARE_KEYCODES:
        raise SystemExit("not enough spare keycodes: %d free, %d reusable, %d keys" % (len(free), len(reusable), len(groups)))
    named = {}
    for name, code in names:
        named.setdefault(int(code), name)
    new_names, lines, replaced = [], [], set()
    for code, group in zip(targets, groups):
        name = named.get(code)
        if name is None:
            name = "Q%03d" % code
            if name in code_of:
                raise SystemExit("keycode name collision")
            new_names.append("    <%s> = %d;" % (name, code))
        elif code in used:
            replaced.add(name)
        group += ["NoSymbol"] * (4 - len(group))
        lines.append('    key <%s> { type= "FOUR_LEVEL", symbols[Group1]= [ %s ] };' % (name, ", ".join(group)))
    kept = symbols
    for name in replaced:
        kept = kept.replace(definitions[name].group(0), "")
    seeded = (keymap[:keycodes_end] + "".join(line + "\n" for line in new_names) + keymap[keycodes_end:symbols_at]
              + kept + "".join(line + "\n" for line in lines) + keymap[symbols_end:])
    target = os.path.join(work, "seeded.xkb")
    with open(target, "w") as output:
        output.write(seeded)
    subprocess.run([xkbcomp, "-w", "0", target, display], check=True, timeout=10,
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"""
