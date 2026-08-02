from app import config


def test_port_config_reads_environment(monkeypatch):
    monkeypatch.setenv("ASSISTANT_KID_HTTP_PORT", "18000")
    monkeypatch.setenv("ASSISTANT_KID_HTTPS_PORT", "18443")

    assert config.http_port() == 18000
    assert config.https_port() == 18443


def test_invalid_port_config_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("ASSISTANT_KID_HTTP_PORT", "70000")
    monkeypatch.setenv("ASSISTANT_KID_HTTPS_PORT", "not-a-port")

    assert config.http_port() == 8000
    assert config.https_port() == 8443


def test_configured_cors_origins_accepts_only_explicit_http_origins(monkeypatch):
    monkeypatch.setenv(
        "ASSISTANT_KID_CORS_ORIGINS",
        " https://phone.example:8443/path, not-a-url, *, http://192.168.1.4:8000 ,http://192.168.1.4:8000",
    )

    assert config.configured_cors_origins() == [
        "https://phone.example:8443",
        "http://192.168.1.4:8000",
    ]


def test_lan_origins_uses_private_ipv4_addresses(monkeypatch):
    import socket

    class Address:
        def __init__(self, family, address):
            self.family = family
            self.address = address

    monkeypatch.setattr(config.psutil, "net_if_addrs", lambda: {
        "en1": [
            Address(socket.AF_INET, "192.168.1.4"),
            Address(socket.AF_INET6, "fe80::1"),
        ],
        "utun0": [Address(socket.AF_INET, "10.0.0.2")],
        "lo0": [Address(socket.AF_INET, "127.0.0.1")],
    })

    assert config.lan_origins() == [
        "http://192.168.1.4:8000",
        "https://192.168.1.4:8443",
    ]


def test_lan_origins_cache_respects_ttl_and_invalidation(monkeypatch):
    import socket

    class Address:
        def __init__(self, address):
            self.family = socket.AF_INET
            self.address = address

    enumerations = []

    def fake_net_if_addrs():
        enumerations.append(1)
        return {"en1": [Address("192.168.1.4")]}

    now = [1000.0]
    monkeypatch.setattr(config.time, "time", lambda: now[0])
    monkeypatch.setattr(config.psutil, "net_if_addrs", fake_net_if_addrs)
    config.invalidate_lan_origins_cache()

    assert config.lan_origins() == [
        "http://192.168.1.4:8000",
        "https://192.168.1.4:8443",
    ]
    assert config.lan_origins() == [
        "http://192.168.1.4:8000",
        "https://192.168.1.4:8443",
    ]
    assert len(enumerations) == 1

    now[0] += config.LAN_ORIGINS_TTL_SECONDS
    assert config.lan_origins()
    assert len(enumerations) == 2

    config.invalidate_lan_origins_cache()
    assert config.lan_origins()
    assert len(enumerations) == 3
