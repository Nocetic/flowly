"""Every supported host maps to one reviewed recipe; everything else to none."""

import ast
import re
from pathlib import Path

import pytest

from flowly.desktop_runtime.install_recipe import (
    PINNED_BROWSER,
    RECIPE_SOURCE,
    recipe_id,
    screen_recipe,
)

APT = {"/usr/bin/apt-get", "/usr/bin/dpkg"}
DNF = {"/usr/bin/dnf", "/usr/bin/rpm"}
UBUNTU_RESTRICTED = APT | {"/proc/sys/kernel/apparmor_restrict_unprivileged_userns", "/usr/sbin/apparmor_parser",
                         "/etc/apparmor.d/abi/4.0"}


def recipe(release, machine="x86_64", paths=APT):
    return screen_recipe(release, machine, lambda path: path in paths)


# The Debian 12 package set this branch has installed and proven since the start.
DEBIAN_PACKAGES = [
    "tigervnc-standalone-server", "xauth", "dbus-x11", "chromium", "chromium-sandbox", "xdotool", "x11-utils",
    "fonts-dejavu-core", "xfwm4", "xfdesktop4", "xfce4-panel", "xfce4-settings", "xfce4-terminal", "xfconf",
    "adwaita-icon-theme", "thunar", "python3", "bash", "nftables",
]


@pytest.mark.parametrize("version", ["12", "13"])
@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
def test_debian_keeps_its_own_signed_chromium(version, machine):
    value = recipe({"ID": "debian", "VERSION_ID": version}, machine)
    assert value == {"recipeVersion": 1, "family": "apt", "distribution": f"debian-{version}", "architecture": machine,
                     "packages": DEBIAN_PACKAGES, "repositories": [],
                     "browser": {"source": "distribution", "package": "chromium"}, "apparmor": False}


@pytest.mark.parametrize("version,label,libraries", [
    ("22.04", "ubuntu-22.04", "22.04"), ("24.04", "ubuntu-24.04", "24.04"),
    ("24.10", "ubuntu-24.10", "24.04"), ("25.04", "ubuntu-25.04", "24.04"),
])
def test_ubuntu_uses_the_pinned_chromium_and_that_releases_libraries(version, label, libraries):
    value = recipe({"ID": "ubuntu", "ID_LIKE": "debian", "VERSION_ID": version}, "aarch64")
    assert value["distribution"] == label
    assert "chromium" not in value["packages"] and "chromium-sandbox" not in value["packages"]
    assert value["browser"] == {"source": "pinned", "version": "145.0.7632.6",
                                "size": PINNED_BROWSER["aarch64"]["size"], "sha256": PINNED_BROWSER["aarch64"]["sha256"]}
    expected = "libasound2t64" if libraries == "24.04" else "libasound2"
    assert expected in value["packages"] and "xfwm4" in value["packages"] and "nftables" in value["packages"]
    assert len(value["packages"]) == len(set(value["packages"]))


def test_ubuntu_gets_the_apparmor_profile_only_where_the_kernel_restricts_namespaces():
    release = {"ID": "ubuntu", "VERSION_ID": "24.04"}
    assert recipe(release, paths=UBUNTU_RESTRICTED)["apparmor"] is True
    assert recipe(release, paths=APT)["apparmor"] is False
    # The knob without the parser cannot be satisfied; the browser would fail, so say so.
    assert recipe(release, paths=APT | {"/proc/sys/kernel/apparmor_restrict_unprivileged_userns"})["apparmor"] is False
    # Ubuntu 22.04 on a newer kernel: the knob, but an AppArmor 3 parser that has no userns rule.
    assert recipe({"ID": "ubuntu", "VERSION_ID": "22.04"}, paths=APT | {
        "/proc/sys/kernel/apparmor_restrict_unprivileged_userns", "/usr/sbin/apparmor_parser"})["apparmor"] is False


@pytest.mark.parametrize("release", [
    {"ID": "linuxmint", "ID_LIKE": "ubuntu debian", "VERSION_ID": "22", "UBUNTU_CODENAME": "noble"},
    {"ID": "pop", "ID_LIKE": "ubuntu debian", "VERSION_ID": "24.04", "UBUNTU_CODENAME": "noble"},
])
def test_ubuntu_derivatives_follow_the_release_they_are_built_on(release):
    assert recipe(release)["distribution"] == "ubuntu-24.04"


@pytest.mark.parametrize("release,machine,paths", [
    ({"ID": "debian", "VERSION_ID": "11"}, "x86_64", APT),          # older than the reviewed releases
    ({"ID": "debian"}, "x86_64", APT),                                # testing/sid: no stable release
    ({"ID": "ubuntu", "VERSION_ID": "20.04"}, "x86_64", APT),
    ({"ID": "ubuntu", "VERSION_ID": "24.04"}, "riscv64", APT),
    ({"ID": "ubuntu", "VERSION_ID": "24.04"}, "x86_64", {"/usr/bin/dpkg"}),  # no apt-get
    ({"ID": "linuxmint", "ID_LIKE": "ubuntu", "UBUNTU_CODENAME": "focal"}, "x86_64", APT),
    ({"ID": "raspbian", "ID_LIKE": "debian", "VERSION_ID": "12"}, "aarch64", APT),
    ({"ID": "fedora", "VERSION_ID": "42"}, "x86_64", APT),          # apt tools on Fedora: not a real host
    ({"ID": "fedora", "VERSION_ID": "40"}, "x86_64", DNF),
    ({"ID": "rocky", "ID_LIKE": "rhel centos fedora", "VERSION_ID": "10.0"}, "x86_64", DNF),  # no Xorg/Xfce at all
    ({"ID": "rocky", "ID_LIKE": "rhel centos fedora", "VERSION_ID": "8.10"}, "x86_64", DNF),
    ({"ID": "rhel", "ID_LIKE": "fedora", "VERSION_ID": "9.4"}, "x86_64", DNF),  # needs a subscription for CRB
    ({"ID": "arch"}, "x86_64", {"/usr/bin/pacman"}),
    ({"ID": "ubuntu", "VERSION_ID": "24.04abc"}, "x86_64", APT),
])
def test_hosts_without_a_reviewed_recipe_get_none(release, machine, paths):
    assert recipe(release, machine, paths) is None


def test_the_recipe_id_binds_every_field():
    value = recipe({"ID": "ubuntu", "VERSION_ID": "24.04"}, paths=UBUNTU_RESTRICTED)
    original = recipe_id(value)
    assert re.fullmatch(r"[a-f0-9]{64}", original) and recipe_id(dict(value)) == original
    for key, changed in [("apparmor", False), ("distribution", "ubuntu-22.04"), ("architecture", "aarch64"),
                         ("packages", value["packages"][:-1]), ("browser", {**value["browser"], "sha256": "0" * 64})]:
        assert recipe_id({**value, key: changed}) != original


def test_the_privileged_source_parses_as_python_3_9():
    # The worker runs under the host's /usr/bin/python3 (Rocky 9 ships 3.9).
    ast.parse(RECIPE_SOURCE, feature_version=(3, 9))


def test_the_pinned_browser_is_the_one_the_screen_extras_playwright_drives():
    requirements = (Path(__file__).parents[1] / "flowly/desktop_runtime/screen-requirements.txt").read_text()
    playwright = re.search(r"^playwright==([\d.]+)", requirements, re.M).group(1)
    # Bumping Playwright needs its Chromium pinned here too (size + SHA-256 per architecture).
    assert {"1.58.0": "145.0.7632.6"}[playwright] == PINNED_BROWSER["version"]
    for machine in ("x86_64", "aarch64"):
        build = PINNED_BROWSER[machine]
        assert re.fullmatch(r"[a-f0-9]{64}", build["sha256"]) and build["size"] > 100_000_000
        assert build["urls"] and all(url.startswith("https://") for url in build["urls"])
        assert all(PINNED_BROWSER["version"] in url or "/1208/" in url for url in build["urls"])


@pytest.mark.parametrize("version", ["41", "42", "43"])
def test_fedora_uses_its_own_chromium_and_adds_no_repository(version):
    value = recipe({"ID": "fedora", "VERSION_ID": version}, "x86_64", DNF)
    assert value["family"] == "dnf" and value["distribution"] == f"fedora-{version}"
    assert value["browser"] == {"source": "distribution", "package": "chromium"} and value["repositories"] == []
    assert "chromium" in value["packages"] and "xprop" in value["packages"] and "Thunar" in value["packages"]


@pytest.mark.parametrize("ident", ["rocky", "almalinux", "centos"])
def test_el9_takes_xfce_and_chromium_from_epel_and_says_so(ident):
    value = recipe({"ID": ident, "ID_LIKE": "rhel centos fedora", "VERSION_ID": "9.6"}, "aarch64", DNF)
    assert value["distribution"] == f"{ident}-9" and value["repositories"] == ["epel", "crb"]
    assert "xorg-x11-utils" in value["packages"] and "xprop" not in value["packages"]


def test_every_recipe_names_its_packages_by_the_distributions_rules():
    import re
    hosts = [({"ID": "debian", "VERSION_ID": "12"}, APT), ({"ID": "ubuntu", "VERSION_ID": "24.04"}, APT),
             ({"ID": "fedora", "VERSION_ID": "42"}, DNF), ({"ID": "rocky", "VERSION_ID": "9.4"}, DNF)]
    for release, paths in hosts:
        value = recipe(release, "x86_64", paths)
        assert all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+._-]{1,127}", name) for name in value["packages"])
        assert len(value["packages"]) == len(set(value["packages"]))
