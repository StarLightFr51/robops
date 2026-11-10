"""robops installed as a folder (robops.exe + _internal): unpacking, swapping while running, moving from one file."""

import hashlib
import io
import os
import zipfile
from pathlib import Path

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


NEW_FOLDER = {"robops.exe": b"MZ-folder-new", "_internal-9.0.0/python312.dll": b"dll-new",
              "_internal-9.0.0/robops/web/app.js": b"js-new", "_internal-9.0.0/base_library.zip": b"lib-new"}


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

@pytest.mark.parametrize("bad", ["../evil.dll", "/abs.dll", "C:/x.dll", "_internal-9.0.0/../../evil.dll",
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

OLD = {"robops.exe": b"MZ-old", "_internal-1.0.0/python312.dll": b"dll-old",
       "_internal-1.0.0/base_library.zip": b"lib-old", "robops-setup.exe": b"setup", "notes.txt": b"mine"}


def test_a_new_version_goes_next_to_the_one_in_use(tmp_path):
    install, new = tmp_path / "install", tmp_path / "new"
    write_tree(install, OLD)
    write_tree(new, NEW_FOLDER)
    updater.swap_in(new, install)
    after = tree(install)
    assert {k: after[k] for k in NEW_FOLDER} == NEW_FOLDER
    assert after["_internal-1.0.0/base_library.zip"] == b"lib-old"      # untouched: robops 1.0 may be running
    assert after["robops.exe.old"] == b"MZ-old"
    assert after["robops-setup.exe"] == b"setup" and after["notes.txt"] == b"mine"     # not ours: untouched
    assert after[updater.CLEANUP_MARKER] == b"_internal-9.0.0"

    updater.cleanup_folder(install)                                     # a later launch
    updater.cleanup_old(install / "robops.exe")
    assert sorted(tree(install)) == sorted([*NEW_FOLDER, "robops-setup.exe", "notes.txt"])


def test_same_version_only_replaces_what_differs(tmp_path, monkeypatch):
    install, new = tmp_path / "install", tmp_path / "new"
    write_tree(install, {**NEW_FOLDER, "_internal-9.0.0/python312.dll": b"damaged"})
    write_tree(new, NEW_FOLDER)
    moved = []
    real_replace = os.replace
    monkeypatch.setattr(updater.os, "replace", lambda a, b: (moved.append(Path(b).name), real_replace(a, b)))
    updater.swap_in(new, install)
    assert moved == ["python312.dll.old", "python312.dll"]             # base_library.zip, robops.exe: identical
    assert tree(install)["_internal-9.0.0/python312.dll"] == b"dll-new"
    updater.cleanup_folder(install)
    assert sorted(tree(install)) == sorted(NEW_FOLDER)


def test_swap_puts_everything_back_on_error(tmp_path, monkeypatch):
    install, new = tmp_path / "install", tmp_path / "new"
    write_tree(install, OLD)
    write_tree(new, NEW_FOLDER)
    before = tree(install)
    real_replace = os.replace

    def exe_locked(src, dst):
        if Path(dst).name == "robops.exe" and str(src).startswith(str(new)):
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(updater.os, "replace", exe_locked)
    with pytest.raises(PermissionError):
        updater.swap_in(new, install)
    assert tree(install) == before                                     # new folder moved back out, exe restored
    assert not (install / updater.CLEANUP_MARKER).exists()


def test_folders_in_use_are_deleted_later(tmp_path, monkeypatch):
    install = tmp_path / "install"
    write_tree(install, {**NEW_FOLDER, "_internal-1.0.0/base_library.zip": b"old", "_internal-0.9.0/x.pyd": b"old",
                         updater.CLEANUP_MARKER: b"_internal-9.0.0"})
    real_rename = type(install).rename

    def busy(self, target):
        if self.name == "_internal-1.0.0":
            raise PermissionError("a running robops 1.0 uses it")
        return real_rename(self, target)

    monkeypatch.setattr(type(install), "rename", busy)
    updater.cleanup_folder(install)
    assert (install / "_internal-1.0.0").is_dir() and not (install / "_internal-0.9.0").exists()
    assert (install / updater.CLEANUP_MARKER).exists()                # try again next time
    monkeypatch.setattr(type(install), "rename", real_rename)
    updater.cleanup_folder(install)                                   # robops 1.0 has stopped
    assert sorted(tree(install)) == sorted(NEW_FOLDER)
    updater.cleanup_folder(install)                                   # nothing to do: no marker, no walk


def test_cleanup_deletes_nothing_without_knowing_the_folder_to_keep(tmp_path):
    write_tree(tmp_path, {"robops.exe": b"MZ", "_internal-1.0.0/a": b"a", "_internal-2.0.0/b": b"b",
                          updater.CLEANUP_MARKER: b""})
    updater.cleanup_folder(tmp_path)
    assert len(updater.contents_dirs(tmp_path)) == 2


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
    assert (single_file.parent / "_internal-9.0.0" / "python312.dll").read_bytes() == b"dll-new"
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
    assert "robops.exe + its libraries folder" in capsys.readouterr().out
    assert tree(setup_env) == {**NEW_FOLDER, "robops-setup.exe": b"MZ-setup"}
    assert reg.path()[0] == rf"C:\Tools;{setup_env}"

    gh = ZipGitHub(tag="v2.3.0")
    assert installer.install(setup_env, session=gh, reg=reg) == 0              # same version, already a folder
    assert "already installed and up to date" in capsys.readouterr().out
    assert not any("assets" in url for url in gh.calls)

    (setup_env / "notes.txt").write_text("mine", encoding="utf-8")
    assert installer.uninstall(setup_env, reg=reg) == 0
    assert sorted(p.name for p in setup_env.iterdir()) == ["notes.txt"]       # libraries gone, user file kept
    assert reg.path()[0] == r"C:\Tools"


def test_installer_moves_a_single_file_install_to_the_folder(setup_env, monkeypatch, capsys):
    setup_env.mkdir(parents=True)
    (setup_env / "robops.exe").write_bytes(b"MZ-single-file")
    monkeypatch.setattr(winsetup, "installed_version", lambda exe: "2.3.0")
    assert installer.install(setup_env, session=ZipGitHub(tag="v2.3.0"), reg=FakeWinreg("")) == 0
    assert "Installing 2.3.0" in capsys.readouterr().out
    assert (setup_env / "_internal-9.0.0" / "python312.dll").is_file()


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
                          "_internal-1.8.0/python312.dll": b"x" * 4096, "notes.txt": b"x" * 9000})
    reg = FakeWinreg("")
    winsetup.register_uninstall(tmp_path, "1.8.0", tmp_path / "robops-setup.exe", reg)
    assert reg.keys[("HKCU", winsetup.UNINSTALL_KEY)]["EstimatedSize"][0] == 6       # KB, not the user's file
