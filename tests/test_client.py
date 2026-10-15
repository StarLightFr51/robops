import pytest

from robops.client import RobloxApiError

from conftest import FakeResponse


def test_publish_rbxl_sends_binary_and_returns_version(make_client, tmp_path):
    place = tmp_path / "game.rbxl"
    place.write_bytes(b"<roblox!binary")
    client, session, _ = make_client([FakeResponse(200, {"versionNumber": 42})])

    result = client.publish_place(111, 222, place)

    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://apis.roblox.com/universes/v1/111/places/222/versions"
    assert call["params"] == {"versionType": "Published"}
    assert call["headers"]["Content-Type"] == "application/octet-stream"
    assert call["data"] == b"<roblox!binary"
    assert session.headers["x-api-key"] == "test-key"
    assert result.version_number == 42


def test_publish_rbxlx_uses_xml_and_saved(make_client, tmp_path):
    place = tmp_path / "game.rbxlx"
    place.write_text("<roblox/>")
    client, session, _ = make_client([FakeResponse(200, {"versionNumber": 3})])

    client.publish_place(1, 2, place, version_type="Saved")

    assert session.calls[0]["headers"]["Content-Type"] == "application/xml"
    assert session.calls[0]["params"] == {"versionType": "Saved"}


def test_publish_rejects_unknown_extension(make_client, tmp_path):
    bad = tmp_path / "game.rbxm"
    bad.write_bytes(b"x")
    client, _, _ = make_client([])
    with pytest.raises(ValueError):
        client.publish_place(1, 2, bad)


def test_retries_on_429_honouring_retry_after(make_client, tmp_path):
    place = tmp_path / "g.rbxl"
    place.write_bytes(b"x")
    client, session, sleeps = make_client([
        FakeResponse(429, {"message": "slow down"}, headers={"Retry-After": "7"}),
        FakeResponse(503, "unavailable"),
        FakeResponse(200, {"versionNumber": 5}),
    ])

    assert client.publish_place(1, 2, place).version_number == 5
    assert len(session.calls) == 3
    assert sleeps[0] == 7.0
    assert len(sleeps) == 2


def test_403_raises_with_scope_and_hint(make_client, tmp_path):
    place = tmp_path / "g.rbxl"
    place.write_bytes(b"x")
    client, _, _ = make_client([FakeResponse(403, {"message": "Publish not allowed on place."})])

    with pytest.raises(RobloxApiError) as err:
        client.publish_place(1, 2, place)

    text = err.value.describe()
    assert err.value.status == 403
    assert "Publish not allowed" in text
    assert "universe-places:write" in text
    assert "Permission manquante" in text


def test_gives_up_after_max_retries(make_client):
    client, session, _ = make_client([FakeResponse(500, "boom")] * 10)
    client.max_retries = 2
    with pytest.raises(RobloxApiError) as err:
        client.restart_servers(9)
    assert err.value.status == 500
    assert len(session.calls) == 3


def test_update_place_builds_update_mask(make_client):
    client, session, _ = make_client([FakeResponse(200, {"displayName": "X"})])

    client.update_place(1, 2, display_name="X", server_size=12)

    call = session.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/cloud/v2/universes/1/places/2")
    assert call["params"] == {"updateMask": "displayName,serverSize"}
    assert call["json"] == {"displayName": "X", "serverSize": 12}


def test_restart_servers_endpoint(make_client):
    client, session, _ = make_client([FakeResponse(200, {})])
    client.restart_servers(77)
    assert session.calls[0]["url"] == "https://apis.roblox.com/cloud/v2/universes/77:restartServers"


def test_set_entry_overwrites_with_allow_missing_and_scope(make_client):
    client, session, _ = make_client([FakeResponse(200, {"revisionId": "r1"})])

    client.set_entry(5, "Game Config", "player/1", {"coins": 10}, scope="eu", users=["users/1"])

    call = session.calls[0]
    assert call["method"] == "PATCH"
    assert call["url"] == (
        "https://apis.roblox.com/cloud/v2/universes/5/data-stores/Game%20Config/scopes/eu/entries/player%2F1"
    )
    assert call["params"] == {"allowMissing": "true"}
    assert call["json"] == {"value": {"coins": 10}, "users": ["users/1"]}


def test_get_entry_with_at_sign_reads_latest(make_client):
    client, session, _ = make_client([FakeResponse(200, {"value": 1})])
    client.get_entry(5, "DS", "a@b")
    assert session.calls[0]["url"].endswith("/entries/a%40b@latest")


def test_list_entry_ids_paginates(make_client):
    client, session, _ = make_client([
        FakeResponse(200, {"dataStoreEntries": [{"id": "a"}, {"id": "b"}], "nextPageToken": "t2"}),
        FakeResponse(200, {"dataStoreEntries": [{"path": "universes/5/data-stores/DS/entries/c"}]}),
    ])

    assert list(client.list_entry_ids(5, "DS", prefix="p")) == ["a", "b", "c"]
    assert session.calls[0]["params"]["filter"] == 'id.startsWith("p")'
    assert session.calls[1]["params"]["pageToken"] == "t2"
