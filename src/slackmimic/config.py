"""Configuration loading: secrets from the environment, mapping from YAML."""

from __future__ import annotations

import json
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
    # After the websocket fails, poll for this long before trying it again.
    websocket_retry_seconds: float = 300.0
    # How far back catch-up polls look for threads with new replies. Replies to
    # parents older than this are only picked up by the websocket.
    thread_lookback_days: float = 7.0
    # In polling mode, how often to run that thread rescan.
    thread_rescan_seconds: float = 300.0
    # On a channel's first run, seed the cursor this many days in the past so
    # recent history is mirrored. 0 = start from now (no backfill).
    backfill_days: float = 0.0
    # Reverse relay (vanta-core -> HS with approval). Off by default.
    reverse_enabled: bool = False
    # Owner's member id in the target workspace; they receive the approval
    # card for every relay candidate (any human member's message).
    owner_member_id: str = ""
    # Target-workspace member ids invited (with the owner) to the dm- channel
    # created for each new HeartStamp member. Empty turns the feature off.
    new_member_dm_invites: list[str] = field(default_factory=list)

    def add_channel(self, channel: ChannelMap) -> None:
        """Start mirroring a channel added while running.

        ``channels`` is shared by the reader, poster and relay, which all look
        mappings up on every use, so they pick the new one up immediately.
        """
        self.channels.append(channel)

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
        websocket_retry_seconds=float(data.get("websocket_retry_seconds", 300.0)),
        thread_lookback_days=float(data.get("thread_lookback_days", 7.0)),
        thread_rescan_seconds=float(data.get("thread_rescan_seconds", 300.0)),
        backfill_days=float(data.get("backfill_days", 0.0)),
        reverse_enabled=bool(data.get("reverse_enabled", False)),
        owner_member_id=str(data.get("owner_member_id", "")),
        new_member_dm_invites=[str(u) for u in data.get("new_member_dm_invites") or []],
    )


def _yaml_scalar(value: str) -> str:
    """``value`` as a YAML scalar: plain if it round-trips, else double-quoted."""
    try:
        if yaml.safe_load(f"k: {value}") == {"k": value}:
            return value
    except yaml.YAMLError:
        pass
    return json.dumps(value)  # a JSON string is a valid double-quoted YAML scalar


def append_channel_mapping(config_path: str, source: str, target: str, label: str) -> None:
    """Append one channel mapping to config.yaml, leaving the rest as written.

    The provision scripts write ``channels`` as the file's last block, so
    appending keeps comments and every other setting. The result is re-parsed
    and must equal the old config plus this mapping; otherwise the file is left
    untouched and :class:`ConfigError` is raised.
    """
    path = Path(config_path)
    before_text = path.read_text(encoding="utf-8")
    before = yaml.safe_load(before_text) or {}
    text = before_text if before_text.endswith("\n") else before_text + "\n"
    text += (
        f"  - source: {_yaml_scalar(source)}\n"
        f"    target: {_yaml_scalar(target)}\n"
        f"    label: {_yaml_scalar(label)}\n"
    )
    entry = {"source": source, "target": target, "label": label}
    expected = dict(before, channels=[*(before.get("channels") or []), entry])
    try:
        after = yaml.safe_load(text)
    except yaml.YAMLError:
        after = None
    if after != expected:
        raise ConfigError(
            f"can't safely add a channel to {config_path}: 'channels' must be its last block"
        )
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
