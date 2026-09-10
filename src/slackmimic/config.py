"""Configuration loading: secrets from the environment, mapping from YAML."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


@dataclass(frozen=True)
class ChannelMap:
    """Maps one source channel to one target channel."""

    source: str  # HeartStamp channel id, e.g. "C0123ABCD"
    target: str  # target workspace channel id, e.g. "C0456WXYZ"
    label: str = ""  # human-friendly name for logs


@dataclass(frozen=True)
class Secrets:
    """Credentials pulled from the environment / .env file."""

    hs_xoxc_token: str
    hs_d_cookie: str
    target_bot_token: str
    # App-level token (xapp-…) for Socket Mode. Only needed for the reverse
    # relay feature; empty otherwise.
    target_app_token: str = ""


@dataclass(frozen=True)
class Config:
    secrets: Secrets
    channels: list[ChannelMap]
    db_path: str = "slackmimic.sqlite3"
    poll_interval_seconds: float = 5.0
    # Prefer the realtime websocket; fall back to polling if it fails.
    use_websocket: bool = True
    # On a channel's first run, seed the cursor this many days in the past so
    # recent history is mirrored. 0 = start from now (no backfill).
    backfill_days: float = 0.0
    # Reverse relay (vanta-core -> HS with approval). Off by default.
    reverse_enabled: bool = False
    # Owner's member id in the target workspace; only their messages are
    # eligible for reverse relay.
    owner_member_id: str = ""

    def target_for(self, source_channel: str) -> Optional[str]:
        for cm in self.channels:
            if cm.source == source_channel:
                return cm.target
        return None

    def source_for(self, target_channel: str) -> Optional[str]:
        """Inverse of target_for: given a target channel, find its source."""
        for cm in self.channels:
            if cm.target == target_channel:
                return cm.source
        return None

    def label_for_target(self, target_channel: str) -> str:
        for cm in self.channels:
            if cm.target == target_channel:
                return cm.label or cm.source
        return target_channel

    @property
    def source_channels(self) -> list[str]:
        return [cm.source for cm in self.channels]


def load_secrets(env_file: Optional[str] = None) -> Secrets:
    """Load required secrets from the environment (optionally from a .env file)."""

    load_dotenv(env_file)

    def require(name: str) -> str:
        value = os.environ.get(name, "").strip()
        if not value:
            raise ConfigError(f"Missing required environment variable: {name}")
        return value

    return Secrets(
        hs_xoxc_token=require("HS_XOXC_TOKEN"),
        hs_d_cookie=require("HS_D_COOKIE"),
        target_bot_token=require("TARGET_BOT_TOKEN"),
        target_app_token=os.environ.get("TARGET_APP_TOKEN", "").strip(),
    )


def load_config(config_path: str, env_file: Optional[str] = None) -> Config:
    """Load full config: channel mapping from YAML plus secrets from env."""

    path = Path(config_path)
    if not path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    data = yaml.safe_load(path.read_text()) or {}

    raw_channels = data.get("channels") or []
    if not raw_channels:
        raise ConfigError("Config must define at least one channel mapping under 'channels'.")

    channels: list[ChannelMap] = []
    for i, entry in enumerate(raw_channels):
        try:
            channels.append(
                ChannelMap(
                    source=str(entry["source"]),
                    target=str(entry["target"]),
                    label=str(entry.get("label", "")),
                )
            )
        except (KeyError, TypeError) as exc:
            raise ConfigError(
                f"channels[{i}] must have 'source' and 'target' keys"
            ) from exc

    return Config(
        secrets=load_secrets(env_file),
        channels=channels,
        db_path=str(data.get("db_path", "slackmimic.sqlite3")),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 5.0)),
        use_websocket=bool(data.get("use_websocket", True)),
        backfill_days=float(data.get("backfill_days", 0.0)),
        reverse_enabled=bool(data.get("reverse_enabled", False)),
        owner_member_id=str(data.get("owner_member_id", "")),
    )
