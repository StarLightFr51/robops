import pytest

from robops.config import ConfigError, load_config

BASE = """
[build]
command = "rojo build -o build/game.rbxl"

[environments.staging]
universe_id = 111
restart_servers = true

[[environments.staging.places]]
name = "main"
place_id = "${STAGING_PLACE:-222}"
file = "build/game.rbxl"
display_name = "Game [STAGING]"

[environments.production]
universe_id = "${PROD_UNIVERSE}"
version_type = "Saved"

[[environments.production.places]]
place_id = 444
file = "build/game.rbxl"
"""


def write(tmp_path, text):
    path = tmp_path / "robops.toml"
    path.write_text(text)
    return path


def test_parses_environment_and_defaults(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    env = cfg.env("staging")
    assert cfg.names == ["staging", "production"]
    assert env.universe_id == 111
    assert env.places[0].place_id == 222          # default value of ${STAGING_PLACE:-222}
    assert env.places[0].file == (tmp_path / "build/game.rbxl").resolve()
    assert env.places[0].has_metadata
    assert env.build_command == "rojo build -o build/game.rbxl"
    assert env.restart_servers is True
    assert env.api_key_env == "ROBLOX_API_KEY"


def test_env_vars_are_resolved_lazily(tmp_path, monkeypatch):
    cfg = load_config(write(tmp_path, BASE))
    # staging works even if PROD_UNIVERSE does not exist
    cfg.env("staging")
    with pytest.raises(ConfigError, match="PROD_UNIVERSE"):
        cfg.env("production")
    monkeypatch.setenv("PROD_UNIVERSE", "999")
    prod = cfg.env("production")
    assert prod.universe_id == 999
    assert prod.version_type == "Saved"
    assert prod.places[0].name == "place1"


def test_unknown_environment(tmp_path):
    cfg = load_config(write(tmp_path, BASE))
    with pytest.raises(ConfigError, match="available: staging, production"):
        cfg.env("dev")


@pytest.mark.parametrize(
    "text, message",
    [
        ("[environments.a]\nuniverse_id = 1\n", "at least one"),
        ("[environments.a]\n[[environments.a.places]]\nplace_id = 1\nfile = 'x.rbxl'\n", "universe_id"),
        ("[environments.a]\nuniverse_id = 0\n[[environments.a.places]]\nplace_id = 1\nfile = 'x.rbxl'\n",
         "greater than 0"),
        ("[environments.a]\nuniverse_id = 1\nversion_type = 'Live'\n"
         "[[environments.a.places]]\nplace_id = 1\nfile = 'x.rbxl'\n", "Published"),
        ("[environments.a]\nuniverse_id = 'abc'\n[[environments.a.places]]\nplace_id = 1\nfile = 'x.rbxl'\n",
         "integer"),
    ],
)
def test_validation_errors(tmp_path, text, message):
    cfg = load_config(write(tmp_path, text))
    with pytest.raises(ConfigError, match=message):
        cfg.env("a")


def test_missing_file_and_bad_toml(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")
    with pytest.raises(ConfigError, match="valid TOML"):
        load_config(write(tmp_path, "[[["))


def test_bundled_template_is_valid_toml_but_requires_ids(tmp_path):
    from importlib import resources
    text = (resources.files("robops") / "templates" / "robops.toml").read_text(encoding="utf-8")
    cfg = load_config(write(tmp_path, text))
    assert cfg.names == ["staging", "production"]
    with pytest.raises(ConfigError, match="greater than 0"):
        cfg.env("staging")


ONE_PLACE = "[[environments.a.places]]\nplace_id = 1\nfile = 'x.rbxl'\n"


@pytest.mark.parametrize("raw, expected", [("false", False), ("False", False), ("0", False), ("", False),
                                           ("true", True), ("yes", True), ("1", True)])
def test_restart_servers_from_env_var_is_parsed_strictly(tmp_path, monkeypatch, raw, expected):
    monkeypatch.setenv("RESTART", raw)
    text = "[environments.a]\nuniverse_id = 1\nrestart_servers = \"${RESTART:-}\"\n" + ONE_PLACE
    assert load_config(write(tmp_path, text)).env("a").restart_servers is expected


def test_restart_servers_rejects_garbage(tmp_path):
    text = "[environments.a]\nuniverse_id = 1\nrestart_servers = 'sometimes'\n" + ONE_PLACE
    with pytest.raises(ConfigError, match="true or false"):
        load_config(write(tmp_path, text)).env("a")


@pytest.mark.parametrize(
    "text, message",
    [
        ("[environments.a]\nuniverse_id = 1\nrestart_server = true\n" + ONE_PLACE,
         "'restart_server' \\(did you mean 'restart_servers'\\?\\)"),
        ("[environments.a]\nuniverse_id = 1\nversoin_type = 'Saved'\n" + ONE_PLACE, "did you mean 'version_type'"),
        ("[environments.a]\nuniverse_id = 1\n" + ONE_PLACE + "server_sise = 10\n", "did you mean 'server_size'"),
        ("[environments.a]\nuniverse_id = 1\n" + ONE_PLACE
         + "[[environments.a.datastores]]\nname = 'C'\nfile = 'c.json'\nscop = 'x'\n", "did you mean 'scope'"),
        ("[environments.a]\nuniverse_id = 1\n[environments.a.players]\nprefix = 'P_'\n" + ONE_PLACE,
         "unknown field"),
        ("[environments.a]\nuniverse_id = 1\nplaces = 'x.rbxl'\n", "list of"),
        ("[environments.a]\nuniverse_id = 1\n" + ONE_PLACE + "display_name = 42\n", "must be text"),
    ],
)
def test_typos_and_wrong_types_are_reported(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text)).env("a")


def test_unknown_root_and_build_fields_fail_at_load(tmp_path):
    with pytest.raises(ConfigError, match="did you mean 'environments'"):
        load_config(write(tmp_path, "[environment.a]\nuniverse_id = 1\n"))
    with pytest.raises(ConfigError, match="did you mean 'command'"):
        load_config(write(tmp_path, "[build]\ncommande = 'rojo build'\n[environments.a]\nuniverse_id = 1\n"))
