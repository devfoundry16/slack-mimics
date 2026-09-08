import pytest

from slackmimic.config import ConfigError, load_config, load_secrets


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("HS_XOXC_TOKEN", "xoxc-test")
    monkeypatch.setenv("HS_D_COOKIE", "d-cookie-test")
    monkeypatch.setenv("TARGET_BOT_TOKEN", "xoxb-test")


def test_load_secrets_ok(env):
    secrets = load_secrets()
    assert secrets.hs_xoxc_token == "xoxc-test"
    assert secrets.hs_d_cookie == "d-cookie-test"
    assert secrets.target_bot_token == "xoxb-test"


def test_load_secrets_missing(monkeypatch):
    monkeypatch.delenv("HS_XOXC_TOKEN", raising=False)
    monkeypatch.delenv("HS_D_COOKIE", raising=False)
    monkeypatch.delenv("TARGET_BOT_TOKEN", raising=False)
    # Point at a nonexistent env file so a developer's real .env isn't read.
    with pytest.raises(ConfigError, match="HS_XOXC_TOKEN"):
        load_secrets(env_file="/nonexistent/.env")


def test_load_config_ok(env, tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
db_path: my.sqlite3
poll_interval_seconds: 3
use_websocket: false
backfill_days: 7
channels:
  - source: C_SRC1
    target: C_DST1
    label: general
  - source: C_SRC2
    target: C_DST2
"""
    )
    cfg = load_config(str(cfg_file))
    assert cfg.db_path == "my.sqlite3"
    assert cfg.poll_interval_seconds == 3.0
    assert cfg.use_websocket is False
    assert cfg.backfill_days == 7.0
    assert cfg.source_channels == ["C_SRC1", "C_SRC2"]
    assert cfg.target_for("C_SRC2") == "C_DST2"
    assert cfg.target_for("C_UNKNOWN") is None


def test_load_config_missing_file(env):
    with pytest.raises(ConfigError, match="not found"):
        load_config("/nonexistent/config.yaml")


def test_load_config_no_channels(env, tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("channels: []\n")
    with pytest.raises(ConfigError, match="at least one channel"):
        load_config(str(cfg_file))


def test_load_config_bad_channel_entry(env, tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("channels:\n  - source: C_ONLY\n")
    with pytest.raises(ConfigError, match=r"channels\[0\]"):
        load_config(str(cfg_file))
