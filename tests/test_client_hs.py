import httpx
import pytest

from slackmimic.client_hs import HeartStampClient


def _client(seen):
    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=b"data")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer xoxc-secret", "Cookie": "d=xoxd-secret"},
    )
    return HeartStampClient("xoxc-secret", "xoxd-secret", client=http)


async def test_download_file_fetches_slack_file():
    seen = []
    hs = _client(seen)
    assert await hs.download_file("https://files.slack.com/files-pri/T1-F1/a.png") == b"data"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/a.png",
        "https://files.slack.com.attacker.example/a.png",
        "https://evilslack.com/a.png",
        "http://files.slack.com/a.png",
        "/api/auth.test",
        "",
    ],
)
async def test_download_file_never_sends_credentials_off_slack(url):
    seen = []
    hs = _client(seen)
    with pytest.raises(ValueError):
        await hs.download_file(url)
    assert seen == []
