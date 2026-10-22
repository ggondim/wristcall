import pytest

from wristcall_cloud.clients import CLIENT_HEADER, parse_client, sees_cloud_entry


def test_client_header_name():
    assert CLIENT_HEADER == "x-wristcall-client"


def test_parse_client():
    assert parse_client("ios/0.3.0") == ("ios", (0, 3, 0))
    assert parse_client(" web/10.0.12 ") == ("web", (10, 0, 12))
    for bad in (None, "", "ios/0.3", "android/1.0.0", "ios/0.3.0 beta", "ios/0.3.0.1", "ios/00000.1.0", "IOS/1.0.0"):
        assert parse_client(bad) is None


@pytest.mark.parametrize(
    "header, platform, expected",
    [
        ("ios/0.3.0", "ios", True),
        ("ios/0.10.0", "ios", True),
        ("ios/1.0.0", "ios", True),
        ("ios/0.2.9", "ios", False),
        ("ios/0.3.0", "web", False),
        ("web/9.0.0", "web", False),
        (None, "ios", False),
        ("garbage", "ios", False),
    ],
)
def test_sees_cloud_entry(header, platform, expected):
    assert sees_cloud_entry(header, platform, {"ios": (0, 3, 0)}) is expected


def test_sees_cloud_entry_platform_must_be_listed():
    assert sees_cloud_entry("web/9.0.0", "web", {"ios": (0, 3, 0)}) is False


def test_sees_cloud_entry_nobody_without_minimums():
    assert sees_cloud_entry("ios/9.9.9", "ios", {}) is False
