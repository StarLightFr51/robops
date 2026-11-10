"""robops installed as a folder (robops.exe + _internal): unpacking, swapping while running, moving from one file."""

import hashlib
import io
import os
import zipfile

import pytest
from test_installer import FakeWinreg

from robops import __version__, cli, installer, updater, winsetup

from test_updater import FakeGitHub


def make_zip(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


NEW_FOLDER = {"robops.exe": b"MZ-folder-new", "_internal/python312.dll": b"dll-new",
              "_internal/robops/web/app.js": b"js-new", "_internal/base_library.zip": b"lib-new"}


class ZipGitHub(FakeGitHub):
    """A release that also has robops-windows.zip."""

    def __init__(self, folder=None, **kw):
        super().__init__(**kw)
        self.files["robops-windows.zip"] = make_zip(folder or NEW_FOLDER)
        sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in self.files.items()
                       if name != "SHA256SUMS.txt")
        self.files["SHA256SUMS.txt"] = sums.encode()


def write_tree(root, files):
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def tree(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# ------------------------------------------------------------------ unpacking

@pytest.mark.parametrize("bad", ["../evil.dll", "/abs.dll", "C:/x.dll", "_internal/../../evil.dll",
                                 "..\\evil.dll"])
def test_zip_paths_cannot_leave_the_folder(tmp_path, bad):
    archive = tmp_path / "r.zip"
    archive.write_bytes(make_zip({**NEW_FOLDER, bad: b"x"}))
    with pytest.raises(updater.UpdateError, match="unsafe path"):
        updater.extract_release_zip(archive, tmp_path / "out")
    assert not (tmp_path / "evil.dll").exists()


def test_zip_must_hold_the_tool(tmp_path):
    archive = tmp_path / "r.zip"
    archive.write_bytes(make_zip({"robops.exe": b"MZ"}))
    with pytest.raises(updater.UpdateError, match="does not contain"):
        updater.extract_release_zip(archive, tmp_path / "out")
    archive.write_bytes(b"not a zip")
    with pytest.raises(updater.UpdateError, match="not a valid zip"):
        updater.extract_release_zip(archive, tmp_path / "out2")


# ------------------------------------------------------------------- swapping

OLD = {"robops.exe": b"MZ-old", "_internal/python312.dll": b"dll-old", "_internal/robops/web/app.js": b"js-old",
       "_internal/gone.pyd": b"gone", "robops-setup.exe": b"setup", "notes.txt": b"mine"}


def test_swap_sets_old_files_aside_then_cleans_them(tmp_path):
    install, new = tmp_path / "install", tmp_path / "new"
    write_tree(install, OLD)
    write_tree(new, NEW_FOLDER)
    updater.swap_in(new, install)
    after = tree(install)
    assert {k: after[k] for k in NEW_FOLDER} == NEW_FOLDER
    assert after["robops-setup.exe"] == b"setup" and after["notes.txt"] == b"mine"     # not ours: untouched
    assert after["robops.exe.old"] == b"MZ-old" and after["_internal/python312.dll.old"] == b"dll-old"
    assert after["_internal/gone.pyd.old"] == b"gone" and "_internal/gone.pyd" not in after
    assert (install / updater.CLEANUP_MARKER).exists()

    updater.cleanup_folder(install)
    updater.cleanup_old(install / "robops.exe")
    assert sorted(tree(install)) == sorted([*NEW_FOLDER, "robops-setup.exe", "notes.txt"])


def test_swap_puts_everything_back_on_error(tmp_path, monkeypatch):
    install, new = tmp_path / "install", tmp_path / "new"
    write_tree(install, OLD)
    write_tree(new, NEW_FOLDER)
    before = tree(install)
    real_replace, moved = os.replace, []

    def flaky(src, dst):
        if str(src).startswith(str(new)):
            moved.append(dst)
            if len(moved) == 3:
                raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(updater.os, "replace", flaky)
    with pytest.raises(PermissionError):
        updater.swap_in(new, install)
    assert tree(install) == before                       # as it was, no .old left


def test_old_files_still_in_use_are_deleted_later(tmp_path, monkeypatch):
    install = tmp_path / "install"
    write_tree(install, {"robops.exe": b"MZ", "_internal/a.dll": b"a", "_internal/a.dll.old": b"old",
                         "_internal/b.pyd.old-123": b"old", updater.CLEANUP_MARKER: b""})
    real_unlink = type(install).unlink

    def busy(self, *args, **kwargs):
        if self.name == "a.dll.old":
            raise PermissionError("loaded by a running robops")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(type(install), "unlink", busy)
    updater.cleanup_folder(install)
    assert sorted(tree(install)) == [updater.CLEANUP_MARKER, "_internal/a.dll", "_internal/a.dll.old", "robops.exe"]
    monkeypatch.setattr(type(install), "unlink", real_unlink)
    updater.cleanup_folder(install)                       # next launch
    assert sorted(tree(install)) == ["_internal/a.dll", "robops.exe"]
    updater.cleanup_folder(install)                       # nothing to do: no marker, no walk


# ------------------------------------------------------------------- releases

def test_install_release_uses_the_folder_when_published(tmp_path):
    gh = ZipGitHub()
    release = updater.latest_release("me/robops", session=gh)
    assert updater.install_release(release, tmp_path / "a", session=gh) == "folder"
    assert tree(tmp_path / "a") == NEW_FOLDER                    # no temporary folder left
    old_release = updater.latest_release("me/robops", session=FakeGitHub())
    assert updater.install_release(old_release, tmp_path / "b", session=FakeGitHub()) == "file"
    assert sorted(tree(tmp_path / "b")) == ["robops.exe"]


@pytest.fixture
def single_file(tmp_path, monkeypatch):
    """A robops.exe of robops <= 1.7 (one file), running."""
    import sys

    exe = tmp_path / "Programs" / "robops" / "robops.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ-single-file")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setenv("ROBOPS_REPO", "me/robops")
    monkeypatch.setenv("ROBOPS_STATE_DIR", str(tmp_path / "state"))
    return exe


def test_self_update_moves_a_single_file_to_the_folder(single_file, monkeypatch, capsys):
    gh = ZipGitHub(tag=f"v{__version__}")                         # same version, but a folder exists
    monkeypatch.setattr(updater, "make_session", lambda token=None: gh)
    assert updater.can_switch_to_folder(updater.latest_release("me/robops", session=gh))
    assert cli.main(["self-update"]) == 0
    out = capsys.readouterr().out
    assert "now installed as a folder" in out and "reinstalled" in out
    assert (single_file.parent / "_internal" / "python312.dll").read_bytes() == b"dll-new"
    assert single_file.read_bytes() == b"MZ-folder-new"
    updater.cleanup_after_update()                                # next launch
    assert not list(single_file.parent.rglob("*.old*")) and not (single_file.parent / updater.CLEANUP_MARKER).exists()

    assert cli.main(["self-update"]) == 0                         # now a folder: nothing to do
    assert "is up to date" in capsys.readouterr().out


def test_daily_check_suggests_the_folder(single_file):
    messages = []
    gh = ZipGitHub(tag=f"v{__version__}")
    assert updater.maybe_auto_update(now=1, session=gh, notify=messages.append) is None
    assert "4 times faster" in messages[0] and single_file.read_bytes() == b"MZ-single-file"


def test_self_update_reports_files_it_cannot_replace(single_file, monkeypatch, capsys):
    gh = ZipGitHub(tag="v99.0.0")
    monkeypatch.setattr(updater, "make_session", lambda token=None: gh)

    def locked(*args, **kwargs):
        raise PermissionError("locked")

    monkeypatch.setattr(updater, "swap_in", locked)
    assert cli.main(["self-update"]) == 1
    assert "Nothing was changed" in capsys.readouterr().err
    assert single_file.read_bytes() == b"MZ-single-file"


# ------------------------------------------------------------------ installer

@pytest.fixture
def setup_env(tmp_path, monkeypatch):
    import sys

    monkeypatch.setenv("ROBOPS_REPO", "me/robops")
    monkeypatch.setenv("ROBOPS_STATE_DIR", str(tmp_path / "state"))
    setup_exe = tmp_path / "Downloads" / "robops-setup.exe"
    setup_exe.parent.mkdir()
    setup_exe.write_bytes(b"MZ-setup")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(setup_exe))
    return tmp_path / "Programs" / "robops"


def test_installer_installs_the_folder_and_uninstall_removes_it(setup_env, monkeypatch, capsys):
    reg = FakeWinreg(r"C:\Tools")
    versions = iter([None, "2.3.0"])
    monkeypatch.setattr(winsetup, "installed_version", lambda exe: next(versions))
    assert installer.install(setup_env, session=ZipGitHub(tag="v2.3.0"), reg=reg) == 0
    assert "robops.exe + _internal" in capsys.readouterr().out
    assert tree(setup_env) == {**NEW_FOLDER, "robops-setup.exe": b"MZ-setup"}
    assert reg.path()[0] == rf"C:\Tools;{setup_env}"

    gh = ZipGitHub(tag="v2.3.0")
    assert installer.install(setup_env, session=gh, reg=reg) == 0              # same version, already a folder
    assert "already installed and up to date" in capsys.readouterr().out
    assert not any("assets" in url for url in gh.calls)

    (setup_env / "notes.txt").write_text("mine", encoding="utf-8")
    assert installer.uninstall(setup_env, reg=reg) == 0
    assert sorted(p.name for p in setup_env.iterdir()) == ["notes.txt"]       # _internal gone, user file kept
    assert reg.path()[0] == r"C:\Tools"


def test_installer_moves_a_single_file_install_to_the_folder(setup_env, monkeypatch, capsys):
    setup_env.mkdir(parents=True)
    (setup_env / "robops.exe").write_bytes(b"MZ-single-file")
    monkeypatch.setattr(winsetup, "installed_version", lambda exe: "2.3.0")
    assert installer.install(setup_env, session=ZipGitHub(tag="v2.3.0"), reg=FakeWinreg("")) == 0
    assert "Installing 2.3.0" in capsys.readouterr().out
    assert (setup_env / "_internal" / "python312.dll").is_file()


def test_install_local_folder_build(tmp_path):
    build = tmp_path / "dist" / "folder" / "robops"
    write_tree(build, NEW_FOLDER)
    install = tmp_path / "Programs" / "robops"
    write_tree(install, {"robops.exe": b"MZ-old"})
    reg = FakeWinreg("")
    assert installer.install_local(build, install, reg=reg) is True
    assert tree(install) == NEW_FOLDER
    with pytest.raises(OSError, match="does not contain"):
        installer.install_local(tmp_path / "dist", install, reg=reg)


def test_apps_and_features_size_counts_the_libraries(tmp_path):
    write_tree(tmp_path, {"robops.exe": b"x" * 1024, "robops-setup.exe": b"x" * 1024,
                          "_internal/python312.dll": b"x" * 4096, "notes.txt": b"x" * 9000})
    reg = FakeWinreg("")
    winsetup.register_uninstall(tmp_path, "1.8.0", tmp_path / "robops-setup.exe", reg)
    assert reg.keys[("HKCU", winsetup.UNINSTALL_KEY)]["EstimatedSize"][0] == 6       # KB, not the user's file
