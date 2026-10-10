import pytest

from wristcall_cloud.audience import AudienceError, is_loopback, normalize_audience


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("HTTPS://Home.Example.com:443/", "https://home.example.com"),
        ("https://x.test/wc/", "https://x.test/wc"),
        ("https://[2001:DB8::1]:8443", "https://[2001:db8::1]:8443"),
        ("http://localhost:8765", "http://localhost:8765"),
        ("http://127.0.0.1:80", "http://127.0.0.1"),
        ("https://xn--bcher-kva.example", "https://xn--bcher-kva.example"),
    ],
)
def test_valid_audiences_are_normalized(url, expected):
    assert normalize_audience(url) == expected
    assert normalize_audience(expected) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.0.10:8765",
        "http://home.example.com",
        "ftp://x",
        "https://",
        "https://u:p@x.test",
        "https://x.test?a=1",
        "https://x.test?",
        "https://x.test#f",
        "x.test",
        "",
        "https://x.test/" + "a" * 2034,  # 2049 characters
        "https://x.test:99999",
        "https://v.exa\tmple",
        "https://x.test\\@evil.test",
        "https://bücher.example",
        "https://x.test.",
        "https://[fe80::1%25en0]",
        "https://x.test/a/../b",
        "https://x.test/%2e",
        "https://x.test//a",
        " https://x.test",
    ],
    ids=lambda url: url if len(url) < 80 else f"{len(url)} characters",
)
def test_invalid_audiences_are_rejected(url):
    with pytest.raises(AudienceError):
        normalize_audience(url)


def test_length_limit_is_2048():
    url = "https://x.test/" + "a" * 2034
    assert len(url) == 2049
    assert normalize_audience(url[:-1]) == url[:-1]


@pytest.mark.parametrize("value", [None, 1, b"https://x.test"])
def test_non_strings_are_rejected(value):
    with pytest.raises(AudienceError):
        normalize_audience(value)


def test_audience_error_is_a_value_error_without_the_url():
    with pytest.raises(ValueError) as e:
        normalize_audience("https://secret-host.example?x=1")
    assert "secret-host" not in str(e.value)


@pytest.mark.parametrize(
    ("audience", "loopback"),
    [
        ("http://localhost:8765", True),
        ("https://127.0.0.1", True),
        ("http://[::1]:8080", True),
        ("https://home.example.com", False),
        ("https://localhost.example.com", False),
        ("https://[2001:db8::1]:8443", False),
    ],
)
def test_is_loopback(audience, loopback):
    assert is_loopback(normalize_audience(audience)) is loopback
