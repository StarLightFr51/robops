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
display_name = "Jeu [STAGING]"

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
    assert env.places[0].place_id == 222          # valeur par défaut de ${STAGING_PLACE:-222}
    assert env.places[0].file == (tmp_path / "build/game.rbxl").resolve()
    assert env.places[0].has_metadata
    assert env.build_command == "rojo build -o build/game.rbxl"
    assert env.restart_servers is True
    assert env.api_key_env == "ROBLOX_API_KEY"


def test_env_vars_are_resolved_lazily(tmp_path, monkeypatch):
    cfg = load_config(write(tmp_path, BASE))
    # staging fonctionne même si PROD_UNIVERSE n'existe pas
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
    with pytest.raises(ConfigError, match="disponibles : staging, production"):
        cfg.env("dev")


@pytest.mark.parametrize(
    "text, message",
    [
        ("[environments.a]\nuniverse_id = 1\n", "au moins une"),
        ("[environments.a]\n[[environments.a.places]]\nplace_id = 1\nfile = 'x.rbxl'\n", "universe_id"),
        ("[environments.a]\nuniverse_id = 0\n[[environments.a.places]]\nplace_id = 1\nfile = 'x.rbxl'\n",
         "strictement positif"),
        ("[environments.a]\nuniverse_id = 1\nversion_type = 'Live'\n"
         "[[environments.a.places]]\nplace_id = 1\nfile = 'x.rbxl'\n", "Published"),
        ("[environments.a]\nuniverse_id = 'abc'\n[[environments.a.places]]\nplace_id = 1\nfile = 'x.rbxl'\n",
         "entier"),
    ],
)
def test_validation_errors(tmp_path, text, message):
    cfg = load_config(write(tmp_path, text))
    with pytest.raises(ConfigError, match=message):
        cfg.env("a")


def test_missing_file_and_bad_toml(tmp_path):
    with pytest.raises(ConfigError, match="introuvable"):
        load_config(tmp_path / "nope.toml")
    with pytest.raises(ConfigError, match="TOML valide"):
        load_config(write(tmp_path, "[[["))


def test_bundled_template_is_valid_toml_but_requires_ids(tmp_path):
    from importlib import resources
    text = (resources.files("robops") / "templates" / "robops.toml").read_text(encoding="utf-8")
    cfg = load_config(write(tmp_path, text))
    assert cfg.names == ["staging", "production"]
    with pytest.raises(ConfigError, match="strictement positif"):
        cfg.env("staging")
