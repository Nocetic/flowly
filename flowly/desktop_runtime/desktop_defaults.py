"""Fixed first-run desktop configuration, executed with the application UID.

The standalone payload needs only system Python: a root agent's checkout or
virtualenv may be deliberately unreadable by the desktop account.
"""

DESKTOP_DEFAULTS_SOURCE = r"""
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

def seed(path, content, mode=0o600):
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise ValueError("invalid desktop configuration")
        return
    fd, temporary = tempfile.mkstemp(prefix=".flowly-seed-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            output.write(content)
            output.flush()
            os.fchmod(output.fileno(), mode)
            os.fsync(output.fileno())
        os.link(temporary, path, follow_symlinks=False)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        os.unlink(temporary)

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
     "done\nexec " + shlex.join([browser, "--user-data-dir=" + profile,
         "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
         "--no-first-run", "--no-default-browser-check"]) + "\n", 0o700)

for name, label, icon, command in (
    ("browser", "Browser", "web-browser", str(launcher)),
    ("terminal", "Terminal", "utilities-terminal", "/usr/bin/xfce4-terminal --disable-server --command=/bin/bash"),
    ("settings", "Settings", "preferences-system", "/usr/bin/xfce4-settings-manager"),
):
    seed(applications / ("flowly-" + name + ".desktop"),
         "[Desktop Entry]\nVersion=1.0\nType=Application\nName=" + label +
         "\nIcon=" + icon + "\nExec=" + command + "\nTerminal=false\nStartupNotify=true\n")

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
