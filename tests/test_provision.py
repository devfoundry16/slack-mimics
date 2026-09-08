import pytest

from slackmimic.provision import (
    ProvisionResult,
    list_source_dms,
    normalize_channel_name,
    provision,
    render_config_yaml,
    short_group_name,
)


def test_normalize_channel_name():
    assert normalize_channel_name("Team Engineering") == "team-engineering"
    assert normalize_channel_name("news.articles.links") == "news-articles-links"
    assert normalize_channel_name("#weird!!name") == "weird-name"
    assert normalize_channel_name("already-fine") == "already-fine"
    assert normalize_channel_name("") == "channel"


def test_render_config_yaml():
    mappings = [("C_SRC1", "C_DST1", "general"), ("C_SRC2", "C_DST2", "random")]
    out = render_config_yaml(mappings, poll_interval_seconds=3, use_websocket=False)
    assert "poll_interval_seconds: 3" in out
    assert "use_websocket: false" in out
    assert "source: C_SRC1" in out
    assert "target: C_DST2" in out
    assert "label: random" in out


class FakeHS:
    def __init__(self, channels):
        self._channels = channels

    async def users_conversations(self, *, types="", limit=200, cursor=None):
        return {"channels": self._channels, "response_metadata": {"next_cursor": ""}}


class FakeTarget:
    def __init__(self, existing=None):
        self.existing = existing or {}
        self.created = []
        self.joined = []
        self._next = 5000

    async def conversations_list(self, *, types="", limit=200, cursor=None):
        return {
            "channels": list(self.existing.values()),
            "response_metadata": {"next_cursor": ""},
        }

    async def conversations_create(self, name, *, is_private=False):
        self._next += 1
        ch = {"id": f"C{self._next}", "name": name, "is_private": is_private, "is_member": True}
        self.created.append(name)
        self.existing[name] = ch
        return {"channel": ch}

    async def conversations_join(self, channel):
        self.joined.append(channel)
        return {"ok": True}


async def test_provision_creates_new_channels():
    hs = FakeHS([
        {"id": "C_A", "name": "general", "is_private": False},
        {"id": "C_B", "name": "secret", "is_private": True},
    ])
    target = FakeTarget()
    result = await provision(hs, target, include_private=True, throttle_seconds=0)
    assert len(result.mappings) == 2
    assert set(result.created) == {"general", "secret"}
    # private source created as private target
    assert target.existing["secret"]["is_private"] is True


async def test_provision_reuses_existing_public_and_joins():
    hs = FakeHS([{"id": "C_A", "name": "general", "is_private": False}])
    target = FakeTarget(existing={"general": {"id": "C_OLD", "name": "general", "is_private": False}})
    result = await provision(hs, target, throttle_seconds=0)
    assert result.mappings == [("C_A", "C_OLD", "general")]
    assert result.reused == ["general"]
    assert target.joined == ["C_OLD"]
    assert not target.created


def test_short_group_name():
    # Drops self (john.oliveira), keeps first 3 others, adds id suffix.
    name = short_group_name(
        "mpdm-bozhidar.hristov--john.oliveira--tega.biokoro--satori.canton-1", "C0C09L0KA1X"
    )
    assert name == "gdm-bozhidar-tega-satori-ka1x"
    assert len(name) <= 80


def test_short_group_name_all_self():
    name = short_group_name("mpdm-john.oliveira-1", "C0ABCDEF")
    assert name.startswith("gdm-group-")


class DMFakeHS:
    def __init__(self, dms, users):
        self._dms = dms
        self._users = users

    async def users_conversations(self, *, types="", limit=200, cursor=None):
        return {"channels": self._dms, "response_metadata": {"next_cursor": ""}}

    async def users_info(self, user):
        name = self._users.get(user, user)
        return {"user": {"profile": {"display_name": name}}}


async def test_list_source_dms_excludes_named_1to1_but_keeps_groups():
    dms = [
        {"id": "D1", "is_im": True, "user": "U_KEITH"},
        {"id": "D2", "is_im": True, "user": "U_ALICE"},
        {"id": "G1", "is_mpim": True, "name": "mpdm-keith--alice--bob-1"},
    ]
    users = {"U_KEITH": "Keith Jones", "U_ALICE": "Alice"}
    hs = DMFakeHS(dms, users)
    out = await list_source_dms(hs, exclude_user_names=["keith"])
    ids = {c.id for c in out}
    # Keith's 1:1 DM excluded; Alice's DM and the group DM (with Keith) kept.
    assert ids == {"D2", "G1"}
    names = {c.id: c.name for c in out}
    assert names["D2"] == "dm-Alice"
    assert names["G1"] == "mpdm-keith--alice--bob-1"
    assert all(c.is_private for c in out)


async def test_provision_existing_private_needs_invite():
    hs = FakeHS([{"id": "C_A", "name": "vip", "is_private": True}])
    target = FakeTarget(
        existing={"vip": {"id": "C_OLD", "name": "vip", "is_private": True, "is_member": False}}
    )
    result = await provision(hs, target, throttle_seconds=0)
    assert result.mappings == [("C_A", "C_OLD", "vip")]
    assert "vip" in result.needs_invite
