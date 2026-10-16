import hashlib
import json
import sys

import pytest

from robops import DEFAULT_REPOSITORY, __version__, cli, updater

NEW_EXE = b"MZ-new-version"


class Resp:
    def __init__(self, status=200, payload=None, content=b"", headers=None):
        self.status_code = status
        self._payload = payload
        self._content = content
        self.headers = headers or {}

    def json(self):
        return self._payload

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._content), 4):
            yield self._content[i:i + 4]


class FakeGitHub:
    """Fake GitHub: a release with robops.exe, robops-setup.exe and SHA256SUMS.txt."""

    def __init__(self, tag="v9.0.0", exe=NEW_EXE, sums=None, latest_status=200, latest_headers=None):
        self.headers = {}
        self.calls = []
        self.files = {"robops.exe": exe, "robops-setup.exe": b"MZ-setup"}
        digest = hashlib.sha256(exe).hexdigest()
        self.files["SHA256SUMS.txt"] = (sums if sums is not None else
                                        f"{digest} *robops.exe\n{'0' * 64}  robops-setup.exe\n").encode()
        self.tag = tag
        self.latest_status = latest_status
        self.latest_headers = latest_headers

    def get(self, url, headers=None, stream=False, timeout=None):
        self.calls.append(url)
        if url.endswith("/releases/latest"):
            if self.latest_status != 200:
                return Resp(self.latest_status, {}, headers=self.latest_headers)
            assets = [{"name": n, "url": f"https://api.github.com/assets/{n}"} for n in self.files]
            return Resp(200, {"tag_name": self.tag, "assets": assets, "html_url": "https://github.com/x"})
        name = url.rsplit("/", 1)[1]
        assert headers == {"Accept": "application/octet-stream"}
        return Resp(200, content=self.files[name])


def test_versions():
    assert updater.parse_version("v1.2.3") == (1, 2, 3)
    assert updater.parse_version("2.0.0-beta.1") == (2, 0, 0)
    assert updater.is_newer("v1.10.0", "1.9.9")
    assert not updater.is_newer("1.2", "1.2.0")
    assert not updater.is_newer("1.1.0", "1.2.0")
    with pytest.raises(updater.UpdateError):
        updater.parse_version("latest")


def test_repository_falls_back_to_project_default(monkeypatch):
    monkeypatch.setattr(updater._build_info, "REPOSITORY", "")
    assert updater.configured_repository() == ""
    assert updater.repository() == DEFAULT_REPOSITORY == "StarLightFr51/robops"
    monkeypatch.setenv("ROBOPS_REPO", "me/robops")
    assert updater.repository() == updater.configured_repository() == "me/robops"


def test_parse_checksums_accepts_sha256sum_formats():
    sums = updater.parse_checksums(f"{'a' * 64} *robops.exe\n{'B' * 64}  robops-setup.exe\nnoise\n")
    assert sums == {"robops.exe": "a" * 64, "robops-setup.exe": "b" * 64}


def test_latest_release_and_errors():
    release = updater.latest_release("me/robops", session=FakeGitHub())
    assert release.version == "9.0.0" and "robops.exe" in release.assets
    with pytest.raises(updater.UpdateError, match="ROBOPS_GITHUB_TOKEN"):
        updater.latest_release("me/robops", session=FakeGitHub(latest_status=404))
    with pytest.raises(updater.UpdateError, match="rate limit"):
        updater.latest_release("me/robops", session=FakeGitHub(
            latest_status=403, latest_headers={"X-RateLimit-Remaining": "0"}))
    with pytest.raises(updater.UpdateError, match="Access denied"):
        updater.latest_release("me/robops", session=FakeGitHub(latest_status=403))
    with pytest.raises(updater.UpdateError, match="Unknown GitHub repository"):
        updater.latest_release("", session=FakeGitHub())


def test_fetch_verified_rejects_corrupted_file(tmp_path):
    gh = FakeGitHub(sums=f"{'f' * 64}  robops.exe\n")
    release = updater.latest_release("me/robops", session=gh)
    with pytest.raises(updater.UpdateError, match="SHA-256"):
        updater.fetch_verified(release, "robops.exe", tmp_path, session=gh)
    assert not (tmp_path / "robops.exe").exists()


def test_replace_running_executable_and_cleanup(tmp_path):
    target = tmp_path / "robops.exe"
    target.write_bytes(b"old")
    (tmp_path / "robops.exe.old").write_bytes(b"older")
    new = tmp_path / "dl" / "robops.exe"
    new.parent.mkdir()
    new.write_bytes(b"new")

    updater.replace_executable(new, target)

    assert target.read_bytes() == b"new"
    assert (tmp_path / "robops.exe.old").read_bytes() == b"old"
    updater.cleanup_old(target)
    assert not list(tmp_path.glob("robops.exe.old*"))


@pytest.fixture
def frozen_exe(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "robops.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ-old-version")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setenv("ROBOPS_REPO", "me/robops")
    monkeypatch.setenv("ROBOPS_STATE_DIR", str(tmp_path / "state"))
    return exe


def test_auto_update_installs_new_version_once_a_day(frozen_exe):
    gh, messages = FakeGitHub(), []
    assert updater.maybe_auto_update(now=1000, session=gh, notify=messages.append) == "9.0.0"
    assert frozen_exe.read_bytes() == NEW_EXE
    assert (frozen_exe.parent / "robops.exe.old").exists()
    assert f"{__version__} → 9.0.0" in messages[0]

    calls = len(gh.calls)
    assert updater.maybe_auto_update(now=1000 + 3600, session=gh, notify=messages.append) is None
    assert len(gh.calls) == calls                       # no new call within 24 h

    updater.cleanup_after_update()
    assert not (frozen_exe.parent / "robops.exe.old").exists()


def test_auto_update_does_nothing_when_up_to_date(frozen_exe):
    gh = FakeGitHub(tag=f"v{__version__}")
    assert updater.maybe_auto_update(now=1, session=gh, notify=lambda m: None) is None
    assert frozen_exe.read_bytes() == b"MZ-old-version"
    assert len(gh.calls) == 1


@pytest.mark.parametrize("var", ["CI", "GITHUB_ACTIONS", "ROBOPS_NO_UPDATE"])
def test_auto_update_disabled_in_ci_or_on_request(frozen_exe, monkeypatch, var):
    monkeypatch.setenv(var, "1")
    gh = FakeGitHub()
    assert updater.maybe_auto_update(now=1, session=gh) is None
    assert gh.calls == []


def test_auto_update_disabled_without_exe(monkeypatch):
    monkeypatch.setenv("ROBOPS_REPO", "me/robops")
    gh = FakeGitHub()
    assert updater.auto_update_disabled_reason() == "installed via pip"
    assert updater.maybe_auto_update(now=1, session=gh) is None and gh.calls == []


def test_auto_update_failure_never_raises(frozen_exe):
    messages = []
    gh = FakeGitHub(sums="")                              # no checksum → refused
    assert updater.maybe_auto_update(now=1, session=gh, notify=messages.append) is None
    assert frozen_exe.read_bytes() == b"MZ-old-version"
    assert "failed" in messages[0]
    state = json.loads((frozen_exe.parent.parent / "state" / "update.json").read_text())
    assert state["last_check"] == 1                       # we don't retry on every command


def test_self_update_command_with_pip_install(capsys):
    assert cli.main(["self-update"]) == 0
    assert "pip" in capsys.readouterr().out
