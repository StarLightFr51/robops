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
    assert entry["UninstallString"][0] == f'"{install_dir / "robops-setup.exe"}" --uninstall --dir "{install_dir}"'
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


def test_uninstall_keeps_files_robops_did_not_create(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ROBOPS_STATE_DIR", str(tmp_path / "state"))
    install_dir = tmp_path / "Users" / "me"          # e.g. setup --uninstall --dir C:\Users\me
    install_dir.mkdir(parents=True)
    for name in ("robops.exe", "robops-setup.exe", "robops.exe.old-123", "notes.txt"):
        (install_dir / name).write_bytes(b"x")
    (install_dir / "Documents").mkdir()
    (install_dir / "Documents" / "thesis.docx").write_bytes(b"precious")

    assert installer.uninstall(install_dir, reg=FakeWinreg("")) == 0

    assert sorted(p.name for p in install_dir.iterdir()) == ["Documents", "notes.txt"]
    assert (install_dir / "Documents" / "thesis.docx").read_bytes() == b"precious"
    assert "kept" in capsys.readouterr().out


def test_uninstall_running_setup_deletes_itself_after_exit(tmp_path, monkeypatch):
    install_dir = tmp_path / "Programs" / "robops"
    install_dir.mkdir(parents=True)
    (install_dir / "robops.exe").write_bytes(b"x")
    me = install_dir / "robops-setup.exe"
    me.write_bytes(b"x")
    monkeypatch.setenv("ROBOPS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(me))
    monkeypatch.setattr(installer, "_is_windows", lambda: True)
    launched = []
    monkeypatch.setattr(installer.subprocess, "Popen", lambda cmd, **kw: launched.append(cmd))

    assert installer.uninstall(install_dir, reg=FakeWinreg("")) == 0

    assert not (install_dir / "robops.exe").exists()      # deleted right away
    assert me.exists()                                    # still running: deleted by cmd
    (command,) = launched
    assert isinstance(command, str)                       # a list would break cmd.exe's quoting
    assert f'del /f /q "{me}"' in command
    assert f'rmdir "{install_dir}"' in command and "/s" not in command


def test_setup_without_dir_targets_its_own_folder(tmp_path, monkeypatch):
    install_dir = tmp_path / "D" / "tools"
    install_dir.mkdir(parents=True)
    (install_dir / "robops.exe").write_bytes(b"x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install_dir / "robops-setup.exe"))
    assert installer._default_install_dir() == install_dir

    # Launched from Downloads (no robops.exe next to it): default folder
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Downloads" / "robops-setup.exe"))
    assert installer._default_install_dir() == winsetup.default_install_dir()


def test_setup_main_install_refuses_outside_windows(monkeypatch, capsys):
    monkeypatch.setattr(installer, "_is_windows", lambda: False)
    assert installer.main(["--quiet"]) == 1
    assert "designed for Windows" in capsys.readouterr().out


def test_setup_main_uninstall_with_dir(tmp_path, monkeypatch, capsys):
    reg = FakeWinreg(r"C:\Tools")
    monkeypatch.setattr(winsetup, "_winreg", lambda r=None: reg)
    monkeypatch.setenv("ROBOPS_STATE_DIR", str(tmp_path / "state"))
    install_dir = tmp_path / "custom"
    install_dir.mkdir()
    (install_dir / "robops.exe").write_bytes(b"x")
    winsetup.add_to_user_path(install_dir, reg)

    assert installer.main(["--uninstall", "--dir", str(install_dir), "--quiet"]) == 0
    assert not install_dir.exists()
    assert reg.path()[0] == r"C:\Tools"
    assert "uninstalled" in capsys.readouterr().out


def test_setup_main_reports_github_errors(monkeypatch, capsys):
    monkeypatch.setattr(installer, "_is_windows", lambda: True)

    def fail(*a, **kw):
        raise updater.UpdateError("No release found")
    monkeypatch.setattr(installer, "install", fail)
    assert installer.main(["--quiet"]) == 1
    assert "✖ No release found" in capsys.readouterr().out
