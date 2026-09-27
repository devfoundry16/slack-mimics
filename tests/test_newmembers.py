import time

import pytest
import yaml

from slackmimic.client_hs import SlackApiError
from slackmimic.config import ChannelMap, Config, ConfigError, Secrets, append_channel_mapping
from slackmimic.newmembers import NewMemberProvisioner
from slackmimic.state.store import StateStore

OWNER = "U_OWNER"
MANISH = "U_MANISH"

CONFIG_YAML = """\
# comments survive
db_path: slackmimic.sqlite3
thread_lookback_days: 1
reverse_enabled: true
owner_member_id: U_OWNER

channels:
  - source: C_SRC
    target: C_DST
    label: general
"""


def member(uid, name, **kw):
    return {"id": uid, "profile": {"display_name": name}, **kw}


class FakeHS:
    def __init__(self, members=()):
        self.members = list(members)
        self.opened = []

    async def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": f"D_{users}"}}

    async def users_list(self, *, limit=200, cursor=None):
        return {"members": self.members, "response_metadata": {"next_cursor": ""}}


class FakeTarget:
    def __init__(self, existing=None, fail_create=False):
        self.existing = existing or {}
        self.created = []
        self.invites = []
        self._fail_create = fail_create
        self._next = 5000

    async def conversations_list(self, *, types="", limit=200, cursor=None):
        return {"channels": list(self.existing.values()), "response_metadata": {"next_cursor": ""}}

    async def conversations_create(self, name, *, is_private=False):
        if self._fail_create:
            raise SlackApiError("conversations.create", "restricted_action")
        self._next += 1
        ch = {"id": f"C{self._next}", "name": name, "is_private": is_private, "is_member": True}
        self.created.append((name, is_private))
        self.existing[name] = ch
        return {"channel": ch}

    async def conversations_join(self, channel):
        return {"ok": True}

    async def conversations_invite(self, channel, users):
        self.invites.append((channel, users))
        if users == OWNER:
            raise SlackApiError("conversations.invite", "already_in_channel")
        return {"ok": True}


@pytest.fixture
async def store(tmp_path):
    s = StateStore(str(tmp_path / "s.sqlite3"))
    await s.connect()
    yield s
    await s.close()


@pytest.fixture
def config_file(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG_YAML)
    return p


def make_config():
    return Config(
        secrets=Secrets("x", "d", "b"),
        channels=[ChannelMap("C_SRC", "C_DST", "general")],
        owner_member_id=OWNER,
        new_member_dm_invites=[MANISH],
    )


def provisioner(hs, target, store, config, config_file):
    return NewMemberProvisioner(hs, target, store, config, str(config_file))


# --- config append -------------------------------------------------------------

def test_append_channel_mapping_keeps_everything_else(config_file):
    append_channel_mapping(str(config_file), "D_NEW", "C_NEW", "dm-Ada Lovelace")
    text = config_file.read_text()
    assert text.startswith("# comments survive\n")
    data = yaml.safe_load(text)
    assert data["thread_lookback_days"] == 1
    assert data["owner_member_id"] == "U_OWNER"
    assert data["channels"][-1] == {"source": "D_NEW", "target": "C_NEW", "label": "dm-Ada Lovelace"}
    assert len(data["channels"]) == 2


def test_append_channel_mapping_quotes_awkward_labels(config_file):
    append_channel_mapping(str(config_file), "D_NEW", "C_NEW", "dm-Ada: #1")
    assert yaml.safe_load(config_file.read_text())["channels"][-1]["label"] == "dm-Ada: #1"


def test_append_channel_mapping_refuses_when_channels_not_last(tmp_path):
    p = tmp_path / "config.yaml"
    original = "channels:\n  - source: C_SRC\n    target: C_DST\n    label: general\ndb_path: x.db\n"
    p.write_text(original)
    with pytest.raises(ConfigError):
        append_channel_mapping(str(p), "D_NEW", "C_NEW", "dm-x")
    assert p.read_text() == original


# --- a member joins --------------------------------------------------------------

async def test_new_member_gets_dm_channel(store, config_file):
    hs, target, cfg = FakeHS(), FakeTarget(), make_config()
    before = time.time()

    ok = await provisioner(hs, target, store, cfg, config_file).on_member_joined(
        member("U_ADA", "Ada Lovelace")
    )

    assert ok is True
    assert hs.opened == ["U_ADA"]
    assert target.created == [("dm-ada-lovelace", True)]
    channel_id = target.existing["dm-ada-lovelace"]["id"]
    # Manish and the owner are invited; "already in channel" is fine.
    assert target.invites == [(channel_id, MANISH), (channel_id, OWNER)]
    # Mirrored from now on, without a restart, starting at "now" (no history).
    assert cfg.target_for("D_U_ADA") == channel_id
    assert float(await store.get_last_ts("D_U_ADA")) >= before - 1
    saved = yaml.safe_load(config_file.read_text())["channels"][-1]
    assert saved == {"source": "D_U_ADA", "target": channel_id, "label": "dm-Ada Lovelace"}
    assert await store.is_known_user("U_ADA")


@pytest.mark.parametrize(
    "user",
    [
        member("U_BOT", "Bot", is_bot=True),
        member("U_GONE", "Gone", deleted=True),
        member("USLACKBOT", "Slackbot"),
    ],
)
async def test_bots_and_deactivated_are_skipped(store, config_file, user):
    hs, target = FakeHS(), FakeTarget()
    ok = await provisioner(hs, target, store, make_config(), config_file).on_member_joined(user)
    assert ok is False
    assert hs.opened == [] and target.created == []
    assert await store.is_known_user(user["id"])


async def test_already_mirrored_dm_is_not_duplicated(store, config_file):
    hs, target = FakeHS(), FakeTarget()
    cfg = make_config()
    cfg.add_channel(ChannelMap("D_U_ADA", "C_EXISTING", "dm-Ada"))

    await provisioner(hs, target, store, cfg, config_file).on_member_joined(member("U_ADA", "Ada"))

    assert target.created == []
    assert [c.source for c in cfg.channels].count("D_U_ADA") == 1
    assert await store.is_known_user("U_ADA")


async def test_name_clash_with_another_mirror_gets_suffix(store, config_file):
    hs, target = FakeHS(), FakeTarget()
    cfg = make_config()
    cfg.add_channel(ChannelMap("D_OTHER", "C_OTHER", "dm-Ada"))

    await provisioner(hs, target, store, cfg, config_file).on_member_joined(member("U0XYZ9ADA", "Ada"))

    assert target.created == [("dm-ada-9ada", True)]


async def test_failure_is_retried_on_next_start(store, config_file):
    hs, target, cfg = FakeHS(), FakeTarget(fail_create=True), make_config()
    original = config_file.read_text()

    ok = await provisioner(hs, target, store, cfg, config_file).on_member_joined(member("U_ADA", "Ada"))

    assert ok is False
    assert not await store.is_known_user("U_ADA")
    assert cfg.target_for("D_U_ADA") is None
    assert config_file.read_text() == original


# --- startup catch-up ------------------------------------------------------------

async def test_first_start_only_records_existing_members(store, config_file):
    hs = FakeHS([member("U_A", "A"), member("U_B", "B")])
    target = FakeTarget()

    n = await provisioner(hs, target, store, make_config(), config_file).reconcile()

    assert n == 0 and target.created == [] and hs.opened == []
    assert await store.is_known_user("U_A") and await store.is_known_user("U_B")


async def test_later_start_provisions_only_new_members(store, config_file):
    await store.add_known_users(["U_A"])
    hs = FakeHS([member("U_A", "A"), member("U_NEW", "Newbie")])
    target = FakeTarget()

    n = await provisioner(hs, target, store, make_config(), config_file).reconcile()

    assert n == 1
    assert hs.opened == ["U_NEW"]
    assert target.created == [("dm-newbie", True)]
