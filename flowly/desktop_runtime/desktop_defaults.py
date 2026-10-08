"""Fixed first-run desktop configuration, executed with the application UID.

The standalone payload needs only system Python: a root agent's checkout or
virtualenv may be deliberately unreadable by the desktop account.
"""

from flowly.desktop_runtime.browser_lifecycle import BROWSER_OPEN_SOURCE

# Dock icons: Lucide globe, square-terminal and settings (ISC licence),
# drawn #f4f4f5 at 64 px. Shipped as PNG: icon themes differ by release (newer
# Adwaita has only grey symbolic glyphs) and an SVG loader is not always present.
DOCK_ICONS = {
    "browser": (
        "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"
    ),
    "terminal": (
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAD3klEQVR4nOybTWjUQBTH32QjtklWRVEPRT0oeulJRBRpFQuK"
        "F22PgmA9WLB+Hiw96MWToEUsbRHqQfTgVSuIerMnPYi2xYPfoCBCD2rdJLul24z/2BbCJtnuNtllxuQHyyQzk928fzJv5r1s"
        "FEo4CiWcVABKOIkXQKUqsW27iTG2uVgUSzxVJYdz/lnTtO/VHMcq6QSjNzgOH8IP7IXxK0hgcI5TMOu5otApXdd/LNZ/UQEs"
        "yzoC4+/C8JUkERDil6KwExBhpFy/sgKYpnkAXZ6R1PBDhmE8DWsNFQAKrjJN6wOu/FqSGj6Ju2AL7MgFtYY6QcvKdwYY/5gx"
        "euQ4ykf3m0ksmKI4WzmnDmwf9FSvM818JzYGAg8K+7ZczhqBsYc9VfcMQz9OEoChex+mHV3YhygPs1m9I6hv6FQG49u8+5wr"
        "d0geSs6Vt4R1DBQgn89vQqF765YtY99IElRV/eLdx1Beg6l8Y2DfoEocUPUCSXQURQm0KY0FKAYwZa7O5ewzudx0M0lGZAFg"
        "vG5Z9gRjfICx4gRmj9MkEZEFwHqhC0XT/C7cBw2apn2eJCGyAAg6Xvhr+U13SJAERBYA4edLXHXfKssdEliQ9JDgxOIEsdY+"
        "h9XWbX8LuwYRLpLAxDYNYqkJX8D7/S3susiOMdZ1AMLOCxChz9/Cr7rRJQlI7AshTAMNQbUkKLEueZE9GoAv8Hl/iHIZn98k"
        "ILEJgHE+DONP+lt4D8LofhKUWATAlR8MMb4XfqGPBCayAAgzdyJp6vPynLOzmBkGSXAiC+A4tKe0bs54TXjjXSLPArre6C6A"
        "FvLvHP6+WxbjXSLfAfDuJub4ZgRAyMGpo4ax/C1JRCxOECL8RDG0WD8IpZhmoRXZ2xYMnQxVBHtnGI1P8BtTVAPqmvqCw+zC"
        "ELmFGaOKpRFHltcaxcY+qgF1TYnB8HZaArj6u3H31OTRXJ1zgmyMlob7xPcP1YC6DgHOG28oip3H1WzF3VCRD8DV/4SHnMMo"
        "a/Ikqq4CZLNsEsUVEog0LU4JJxWAEk4qACWcVABKOKkAlHBSASjhBAZDiNZmSW58kWOYTYF3QENDw1cUM946x3HWkyTA2KaS"
        "qhnYFPgvt0AB5mPv8ZIv3U+SUCw6bSVV42H5hNB8ABIWY8jb7fDsX8Kz/mnOM69IYBib3YWi11uHc38T2j+soVAobCsWZ19j"
        "UyO5sVU1sx1D4H1QY+gs4B6AVNQxkhzXhjDj/7WXO1jTtAcYBu1zb2HIhfvChHvurg3l+lXzygzy+bxFkldmRjMZ1l3J+0NV"
        "/3MjkS9N/c+ksQAlnFQASjh/AQAA//++9+lhAAAABklEQVQDAACNXk7q0//wAAAAAElFTkSuQmCC"
    ),
    "settings": (
        "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAKL0lEQVR4nOybDZAcRRXH3+zeJne3uyAE5UOB05AQxJAvRIgf"
        "5DCGSgJBgoiKlJyWoJQpoSwNZYwfqFSqsIKfVRo/gsQQJKghJgaCmgSpSiFCDhNjQooEAgFCEQR39y65u93x99iN7vR1z87e"
        "3myp4V+1tTNvXne/97r79evXMwk5wpGQIxxHvAFapMnI5Q6dKTIwjcvxnuePUZrve7v424o4G7LZkdulifCkSfB9P9HT07PQ"
        "92Uht0kH24DnyVfa29sXeZ5XkiagaQbI5fKbUOo90bj9DZlM5gJpApriA/L5/LeiK6/wOilzizQBsY8Ahv07SyX/QcujNUyH"
        "7so1fsGfjpGy1QyJhDeV6bBZYkTsTrBUKs0z7Yw76Mxm2zYGaf4x+XzhIYwwJlhWYjVA7FOAXh5v3H/dVF6B4v+gxxcb5DMl"
        "ZsRuABR7a/A+scnFWyoldhqlz5KYEasBent7O0waw3+/ix/jvGip41SJEbEagDk806RlMiOecvGn0yOettTxPokRda0ChUJh"
        "Nn8XMo87+PUzvHcRza1Mp9OPmLx4/3Px/r/nMl1F3pLJpCeHtUG88LfqaUM7+WTSey+rwZ/tbcilONDTCKBS/J6EfB/yrJWI"
        "iGQAFJ+DIDdxOcH2nGerqWpFJtN2P0Kdg0Af5v4qS3Ofy2TaF4e1xfo/H75Flke309IKlHu4UOidwbXWP9NRzWMYYyG8v5Ua"
        "qGkAFJpLT94tDcYMGGWH9j6921uDL53P93SjwGnSGHzquLjWaAj1AfT821F+uTSsvOxOpVrm1FJeAU8hlUpepGWkMXjUsRId"
        "poQxhRqA+XUnf60GeR+W/T499YBEkcKTL2Wz6dGtra27JCLg3allKPvVaCX8jSoTF88aD9rQ4ZdhJZ09yzBkHvt3GA0FNinl"
        "6K3nBhzhNVR1/H/okk8k5LZkMvndehS34eDBg2OLxeI86uySgEP197ONXoJPuVWDqIo8rchzP8Z4V7AW7yPwrbDVH2KAgjqQ"
        "i6pIz6XT7eNo7J82fnzFG/k7hefPt7W17ZEYgDHewrL4Bi6fZlXYZ+PBCEcx7B83OmQ1I+oSG7/nqGRUodATCEqw6qdxKD+U"
        "/wHkcoXrkPcH1TQ67xg652WT1+oDCoW+k0waFr9dhhEY2Tt06NA4/em1DCMY7stMWl9f34k2XutukIhsX6EwEKBhvR5pEDqE"
        "mc834JimMMI0pmhXen//QIFeY+32H8Zv3MoUekoaQ9EkjBgxwjplrCMAZV+iV3LVNHV20gCYl9cODBT/ynz8DMPzPKkoX0Ea"
        "2lRa/iw8W+H9uDQA/FFAVnR5xeW7nMsgAv0sSPEXE6V9Rz2/1AH4j8Wh3ovi6j/Stfg1KQLvTylDwsR/ndQB+I9GxuWU/0aw"
        "TlnqbM/1gF44kYr2yuBpsoegZtbIkSN3SAQwtFchwCUyBND+Krz3pVF4UXwi6tzD5SnGoz5qOpXl+3lbuVDnk8v1XM68vEsG"
        "Cea/ghEms8bvDheq90rCqV9YHu1BuXVsfzeX6ytNxUga13cMZnWv4YeBbzmdqbOFyzbzGUmWy3Hgd7vK1vS+jIRZCGuLp7uJ"
        "7Se5ymlcUCyWtjOkjzpMU8Mh0OcRaCn0gJflWQtlugi9b+HZ0VX0lzHOGa4eVLCD/Dtlxpl0ys1m6f6dhKBmPkArSCYT5yPK"
        "C8ajiblcb6erHIJ3VStfEWge9f3YVL78zBsoP5PrDbr6gY+52tEOGqy8vx9Dv6OW8opICRGWpQd8P9WpIW6gcKI011WGpc7Y"
        "Ovt/oBeXSQ3AcxvGM9NmE1z8yHRZ8N7PpVKpabb8gQ2RM0KVI6t1RuNhW9ZJQV7v1xIZiV8Z7Uxy8/qBlBkjaHVUB/1qS1IH"
        "cIh/MUjH2/johXYEGV1NI6vTLRGRTMqjwXa9sdSZsnN7owzCY1IHGs0J1hPC+jJ0qJxRzzDqCqvrNIBn5ulfsHIRNtNjgW0w"
        "9xMlIopFmWyU3eFOpvgHDILTX9gQ2QC6Q+RvepAmzpidKbDFKP+pKJse5eFg+JNhdRkt7TVkms0G6wyJiEgG0Owrmxedw4Fd"
        "IvP6vpBixpz3zqKea6QG2HNcB+94g/yoix97BWTQGKKvr38TwdEYiYAoSdHLKklRo2F/ezabcR5daShNuZ3VB54aCBFTdLFE"
        "/cbR1lyCp6VG8KSB0DiWR+eBCoGQTpHTTbqG4MQCqyUEoQbQXZluTEx6ea1tOZtQ+PGw8q5QmDrVoBtJmz3Jf0up5J0MtROB"
        "5w7m9a7IZtvvCmtHcwpsqXWFsmy2/Ksx3s9dZZ0G0F0cw1730GZSlDi+ZQ5xwTaJAHZ1Go3NlCEAQ93DZuj9UXhDNkO9ZINO"
        "ZoQcsJVz+gB6f4EYyqtAVDYxqvIK+D/KX82Q1IK1bIK6ojLTy920NUEzyUYuo42DlC+4yjlHAPPqJaxWtff376CRK2WIoIe6"
        "MOC3zf2BiXLyQq7XkFiG3FYvRi8tq6rzRfzV6228YVnhQOCCdUdppkgagEaIODpNq8/AQc447LjK67ys5349DnJDo+k3TY8z"
        "fQNxAztXq65WIr11Ao+eq6ZhgPRw5AWroUffpLn78dTPyjCC6XsSoy2QA0T+42x+wOoDdO9t7vzouatkmKHJz+FWXsHK8aHg"
        "vS6ldTpBNj5rgpW86lyyLn5NgGjARK++WWKCZpW1jcohjBV6MEKG6cYg1Vvp4k+4K0qYwcoJDK1VJh85v5vwF/uY08/w20wg"
        "sxtajt+PokZjYdCjMer/Cb88aa8ntA1ti/tnaONrQZn1aKywht4OODxSb87zwdBAiMr0iKvDIDO3fLUoxvM+ITUyvTi3BQzz"
        "m2UIwOBfZOR9M4xHl7xKBhun7X1QjHAd8lamtPNdo1qR4BQE0FPgdmkM63BCV9AzuSjM5fO9Ho3+LpTG0KMHpXSAczMVuhnS"
        "V1+oQK3a6Hu7M1FoeVRmDK8ZoUaVLyH7B8KUV0RJiq6lorO5XO/i0Ryepp/p5WPhnQXFlvu7uHzkHg6WYBKg3nRLK8u0bl3O"
        "NFVOm38KqWa9yozs66QG6sqe5PMHEWzgaqbFmyh6QF+SwtB3ahhq8uKpz6u8JPXv6UO5B4nt3x3WBo5tM8KfW1VGX5KaToD0"
        "kKWNcyovSY2Fk0BNWL6TS7LZtj9KRMT6rjAHK5wD+t+rptGD7a7sjr4fxFQJxB8odS09uURiQqzvCaZSiUFDkGPqDhd/Pt83"
        "KIbAWPdKjIjVAOQLnjBphL7HufhbWkpmhlffS9grMaIZ3wsE/EOxWBztYsQ4Yw1SqAcfDjTDAEZyVG60HXvrsTsObb5BjnyW"
        "MFTE/sFEJZgKHKiUExaerhDbKjJYP5ioLGWPSIxoyjdDrP+LUHB+PWUw2s0smQskZjTzo6mN9PD5UXg1sCKDM02agKZ9OElG"
        "5gKG9JfF8gJTFfSzuYXKK01C00bAYZQ/nCx20s9vq/pwUtPr2/6vP5z8b8VrH0/LEY5/AQAA//8YF/v/AAAABklEQVQDAMmn"
        "pakdatLnAAAAAElFTkSuQmCC"
    ),
}

DESKTOP_DEFAULTS_SOURCE = ("BROWSER_OPEN_SOURCE = " + repr(BROWSER_OPEN_SOURCE) + "\n"
                           + "DOCK_ICONS = " + repr(DOCK_ICONS) + "\n") + r"""
import base64
import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path

if os.geteuid() == 0:
    raise SystemExit("desktop defaults require the application account")

def directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError("invalid application directory")
    return path

def seed(path, content, mode=0o600, *, managed=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError("invalid desktop configuration")
        if not managed:
            return
    fd, temporary = tempfile.mkstemp(prefix=".flowly-seed-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb" if isinstance(content, bytes) else "w") as output:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        if managed:
            os.replace(temporary, path)
        else:
            os.link(temporary, path, follow_symlinks=False)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        Path(temporary).unlink(missing_ok=True)

config = directory(Path(os.environ["XDG_CONFIG_HOME"]))
data = directory(Path(os.environ["XDG_DATA_HOME"]))
runtime = directory(Path(os.environ["XDG_RUNTIME_DIR"]))
applications = directory(data / "applications")
xfce = directory(config / "xfce4")
xfconf = directory(xfce / "xfconf")
channels = directory(xfconf / "xfce-perchannel-xml")
browser, profile = sys.argv[1:]
if not Path(browser).is_absolute() or not Path(profile).is_absolute():
    raise ValueError("application paths must be absolute")
launcher = runtime / "launch-browser"
if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-" for character in str(launcher)):
    raise ValueError("invalid launcher path")
seed(launcher, "#!/bin/sh\n"
     "for window in $(/usr/bin/xdotool search --class '^chromium$' 2>/dev/null); do\n"
     "  case \"$(/usr/bin/xprop -id \"$window\" _NET_WM_WINDOW_TYPE 2>/dev/null)\" in\n"
     "    *_NET_WM_WINDOW_TYPE_NORMAL*)\n"
     "      /usr/bin/xdotool windowmap \"$window\" windowactivate --sync \"$window\" && exit 0;;\n"
     "  esac\n"
     "done\nexec " + shlex.join(["/usr/bin/python3", "-I", "-c", BROWSER_OPEN_SOURCE]) + "\n",
     0o700, managed=True)

icons = directory(data / "flowly-dock")
for name, label, command in (
    ("browser", "Browser", str(launcher)),
    ("terminal", "Terminal", "/usr/bin/xfce4-terminal --disable-server --command=/bin/bash"),
    ("settings", "Settings", "/usr/bin/xfce4-settings-manager"),
):
    icon = icons / (name + ".png")
    if "\n" in str(icon) or "\r" in str(icon):
        raise ValueError("invalid icon path")
    # Flowly's own entries: rewritten on every start so a release can change them.
    seed(icon, base64.b64decode(DOCK_ICONS[name]), managed=True)
    seed(applications / ("flowly-" + name + ".desktop"),
         "[Desktop Entry]\nVersion=1.0\nType=Application\nName=" + label +
         "\nIcon=" + str(icon) + "\nExec=" + command + "\nTerminal=false\nStartupNotify=true\n", managed=True)

seed(channels / "xfce4-desktop.xml", '''<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-desktop" version="1.0">
  <property name="desktop-icons" type="empty"><property name="style" type="int" value="0"/></property>
</channel>
''')
seed(channels / "xsettings.xml", '''<?xml version="1.0" encoding="UTF-8"?>
<channel name="xsettings" version="1.0">
  <property name="Net" type="empty">
    <property name="ThemeName" type="string" value="Adwaita-dark"/>
    <property name="IconThemeName" type="string" value="Adwaita"/>
  </property>
  <property name="Gtk" type="empty"><property name="FontName" type="string" value="Sans 10"/></property>
</channel>
''')
seed(channels / "xfce4-panel.xml", '''<?xml version="1.0" encoding="UTF-8"?>
<channel name="xfce4-panel" version="1.0">
  <property name="configver" type="int" value="2"/>
  <property name="panels" type="array">
    <value type="int" value="1"/>
    <property name="dark-mode" type="bool" value="true"/>
    <property name="panel-1" type="empty">
      <property name="position" type="string" value="p=10;x=0;y=0"/>
      <property name="position-locked" type="bool" value="true"/>
      <property name="length" type="uint" value="1"/>
      <property name="length-adjust" type="bool" value="true"/>
      <property name="size" type="uint" value="48"/>
      <property name="icon-size" type="uint" value="32"/>
      <property name="autohide-behavior" type="uint" value="0"/>
      <property name="plugin-ids" type="array">
        <value type="int" value="1"/><value type="int" value="2"/><value type="int" value="3"/>
      </property>
    </property>
  </property>
  <property name="plugins" type="empty">
    <property name="plugin-1" type="string" value="launcher"><property name="items" type="array"><value type="string" value="flowly-browser.desktop"/></property></property>
    <property name="plugin-2" type="string" value="launcher"><property name="items" type="array"><value type="string" value="flowly-terminal.desktop"/></property></property>
    <property name="plugin-3" type="string" value="launcher"><property name="items" type="array"><value type="string" value="flowly-settings.desktop"/></property></property>
  </property>
</channel>
''')
gtk = directory(config / "gtk-3.0")
seed(gtk / "gtk.css", '''
/* Only the remote desktop dock; application widgets retain their own theme. */
.xfce4-panel {
  background-color: #24262b;
  border: 1px solid #41444c;
  border-radius: 14px;
}
.xfce4-panel button {
  background: transparent;
  border: none;
  border-radius: 10px;
  box-shadow: none;
}
.xfce4-panel button:hover { background-color: #41444c; }
''')
"""
