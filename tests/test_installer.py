import sys

import pytest

from robops import installer, updater, winsetup

from test_updater import NEW_EXE, FakeGitHub

HKCU = "HKCU"


class FakeWinreg:
    """Simulated Windows registry (HKCU only)."""

    HKEY_CURRENT_USER = HKCU
    KEY_READ, KEY_SET_VALUE = 1, 2
    REG_SZ, REG_EXPAND_SZ, REG_DWORD = 1, 2, 4

    def __init__(self, path=None, kind=2):
        self.keys = {(HKCU, "Environment"): {}}
        if path is not None:
            self.keys[(HKCU, "Environment")]["Path"] = (path, kind)

    class _Handle:
        def __init__(self, store):
            self.store = store

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, sub, reserved=0, access=0):
        if (root, sub) not in self.keys:
            raise FileNotFoundError(sub)
        return self._Handle(self.keys[(root, sub)])

    def CreateKey(self, root, sub):
        return self._Handle(self.keys.setdefault((root, sub), {}))

    def DeleteKey(self, root, sub):
        if (root, sub) not in self.keys:
            raise FileNotFoundError(sub)
        del self.keys[(root, sub)]

    def QueryValueEx(self, handle, name):
        if name not in handle.store:
            raise FileNotFoundError(name)
        return handle.store[name]

    def SetValueEx(self, handle, name, reserved, kind, value):
        handle.store[name] = (value, kind)

    def path(self):
        return self.keys[(HKCU, "Environment")].get("Path")


def test_user_path_add_is_idempotent_and_keeps_type():
    reg = FakeWinreg(r"%USERPROFILE%\bin;C:\Tools", kind=FakeWinreg.REG_EXPAND_SZ)
    assert winsetup.add_to_user_path(r"C:\Users\Alex\AppData\Local\Programs\robops", reg)
    value, kind = reg.path()
    assert value == r"%USERPROFILE%\bin;C:\Tools;C:\Users\Alex\AppData\Local\Programs\robops"
    assert kind == FakeWinreg.REG_EXPAND_SZ
    # same folder, different case and trailing backslash: no duplicate
    assert not winsetup.add_to_user_path("c:\\users\\alex\\appdata\\local\\programs\\ROBOPS\\", reg)
    assert winsetup.remove_from_user_path(r"C:\Users\Alex\AppData\Local\Programs\robops", reg)
    assert reg.path()[0] == r"%USERPROFILE%\bin;C:\Tools"
    assert not winsetup.remove_from_user_path(r"C:\Nope", reg)


def test_user_path_created_when_missing():
    reg = FakeWinreg(path=None)
    assert winsetup.add_to_user_path(r"C:\robops", reg)
    assert reg.path() == (r"C:\robops", FakeWinreg.REG_EXPAND_SZ)


@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBOPS_REPO", "me/robops")
    monkeypatch.setenv("ROBOPS_STATE_DIR", str(tmp_path / "state"))
    setup_exe = tmp_path / "Downloads" / "robops-setup.exe"
    setup_exe.parent.mkdir()
    setup_exe.write_bytes(b"MZ-setup")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(setup_exe))
    return tmp_path / "Programs" / "robops"


def test_install_then_uninstall(setup_env, monkeypatch, capsys):
    install_dir, reg, gh = setup_env, FakeWinreg(r"C:\Tools"), FakeGitHub(tag="v2.3.0")
    monkeypatch.setattr(winsetup, "installed_version", lambda exe: None)

    assert installer.install(install_dir, session=gh, reg=reg) == 0

    assert (install_dir / "robops.exe").read_bytes() == NEW_EXE
    assert (install_dir / "robops-setup.exe").read_bytes() == b"MZ-setup"
    assert reg.path()[0] == rf"C:\Tools;{install_dir}"
    entry = reg.keys[(HKCU, winsetup.UNINSTALL_KEY)]
    assert entry["DisplayVersion"] == ("2.3.0", FakeWinreg.REG_SZ)
    assert entry["UninstallString"][0] == f'"{install_dir / "robops-setup.exe"}" --uninstall'
    assert not list(install_dir.glob(".download-*"))
    out = capsys.readouterr().out
    assert "NEW terminal" in out and "SHA-256 checksum verified" in out

    assert installer.uninstall(install_dir, reg=reg) == 0
    assert reg.path()[0] == r"C:\Tools"
    assert (HKCU, winsetup.UNINSTALL_KEY) not in reg.keys
    assert not install_dir.exists()


def test_install_uses_default_repo_when_none_is_burned_in(setup_env, monkeypatch):
    monkeypatch.delenv("ROBOPS_REPO")
    monkeypatch.setattr(updater._build_info, "REPOSITORY", "")
    monkeypatch.setattr(winsetup, "installed_version", lambda exe: None)
    gh = FakeGitHub(tag="v2.3.0")

    assert installer.install(setup_env, session=gh, reg=FakeWinreg("")) == 0

    assert "/repos/StarLightFr51/robops/releases/latest" in gh.calls[0]
    assert (setup_env / "robops.exe").read_bytes() == NEW_EXE


def test_install_skips_download_when_up_to_date(setup_env, monkeypatch, capsys):
    gh = FakeGitHub(tag="v2.3.0")
    monkeypatch.setattr(winsetup, "installed_version", lambda exe: "2.3.0")
    assert installer.install(setup_env, session=gh, reg=FakeWinreg("")) == 0
    assert "already installed and up to date" in capsys.readouterr().out
    assert not any("assets" in url for url in gh.calls)


def test_install_updates_older_version(setup_env, monkeypatch, capsys):
    setup_env.mkdir(parents=True)
    (setup_env / "robops.exe").write_bytes(b"MZ-1.0")
    monkeypatch.setattr(winsetup, "installed_version", lambda exe: "1.0.0")
    assert installer.install(setup_env, session=FakeGitHub(tag="v2.3.0"), reg=FakeWinreg("")) == 0
    assert "Updating 1.0.0 → 2.3.0" in capsys.readouterr().out
    assert (setup_env / "robops.exe").read_bytes() == NEW_EXE


def test_install_reports_missing_release(setup_env, monkeypatch):
    monkeypatch.setattr(winsetup, "installed_version", lambda exe: None)
    with pytest.raises(updater.UpdateError):
        installer.install(setup_env, session=FakeGitHub(latest_status=404), reg=FakeWinreg(""))
    assert not (setup_env / "robops.exe").exists()


def test_install_local_build(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "robops.exe").write_bytes(b"MZ-local")
    (dist / "robops-setup.exe").write_bytes(b"MZ-setup")
    install_dir = tmp_path / "Programs" / "robops"
    reg = FakeWinreg(r"C:\Tools")

    assert installer.install_local(dist / "robops.exe", install_dir, setup_exe=dist / "robops-setup.exe",
                                   version="1.1.0", reg=reg) is True
    assert (install_dir / "robops.exe").read_bytes() == b"MZ-local"
    assert reg.path()[0] == rf"C:\Tools;{install_dir}"
    assert reg.keys[(HKCU, winsetup.UNINSTALL_KEY)]["DisplayVersion"][0] == "1.1.0"

    # Recompile then reinstall: replaces the exe, no duplicate in the PATH
    (dist / "robops.exe").write_bytes(b"MZ-local-v2")
    assert installer.install_local(dist / "robops.exe", install_dir, reg=reg) is False
    assert (install_dir / "robops.exe").read_bytes() == b"MZ-local-v2"
    assert sorted(p.name for p in install_dir.iterdir()) == ["robops-setup.exe", "robops.exe"]
