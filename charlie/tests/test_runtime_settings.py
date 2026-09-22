"""device.ota_url 设置参数测试

背景：OTA 服务器地址原本是编译进固件的 CONFIG_OTA_URL（硬编码 LAN IP，
Mac 换 IP 设备就找不到服务器）。本次把它收进设置中心：设置页写
runtime_settings.json → OTA 响应的 device 段下发（仅非空时）→ 固件按 key
自动存入 NVS "device" → GetCheckVersionUrl 优先读它。服务器搬家可以在
设备还能连上旧服务器的前提下提前改好，设备下次激活即用新地址。

锁死的契约：
1. 参数在 schema 里、类型 str、默认空串（空 = 不干预设备已保存地址）；
2. POST 校验 http(s) 开头，畸形 URL 在落盘前被拒（否则设备激活直接失败）；
3. OTA device 段：空值不下发（空串会把设备导向空地址）、非空才下发；
4. 显式清空（覆盖里存空串）等价于不下发。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import voice_server
    return TestClient(voice_server.app)


@pytest.fixture(autouse=True)
def clean_settings():
    """每条用例前后都清空覆盖，避免 store 残留互相污染"""
    from app import runtime_settings
    runtime_settings.clear()
    yield
    runtime_settings.clear()


class TestOtaUrlSchema:
    def test_in_schema_with_str_type(self):
        from app.runtime_settings import schema
        spec = schema()["device.ota_url"]
        assert spec["type"] == "str"

    def test_default_empty_string(self):
        # 空默认 = 不干预：设备沿用已保存地址/编译默认值
        from app.runtime_settings import default_for
        assert default_for("device.ota_url") == ""

    def test_get_schema_api_exposes_param(self, client):
        r = client.get("/api/settings")
        assert r.status_code == 200
        assert "device.ota_url" in r.json()["settings"]


class TestOtaUrlValidation:
    def test_valid_http_url_accepted(self, client):
        r = client.post("/api/settings",
                        json={"device.ota_url": "http://192.168.1.99:8000/xiaozhi/ota"})
        body = r.json()
        assert body["ok"] is True
        from app.runtime_settings import get
        assert get("device.ota_url", "") == "http://192.168.1.99:8000/xiaozhi/ota"

    def test_valid_https_url_accepted(self, client):
        r = client.post("/api/settings",
                        json={"device.ota_url": "https://charlie.example.com/xiaozhi/ota"})
        assert r.json()["ok"] is True

    def test_invalid_scheme_rejected_before_persist(self, client):
        # 畸形 URL 一旦落盘，设备激活会拿着它去轮询并直接失败，且报错难定位
        r = client.post("/api/settings", json={"device.ota_url": "ftp://host/ota"})
        body = r.json()
        assert body["ok"] is False
        assert any("http" in e for e in body["errors"])
        from app.runtime_settings import overrides
        assert "device.ota_url" not in overrides()

    def test_empty_url_accepted(self, client):
        # 留空是合法操作（回到不下发状态），不应报错
        r = client.post("/api/settings", json={"device.ota_url": ""})
        assert r.json()["ok"] is True


class TestOtaDeviceOffer:
    def test_omitted_when_unset(self, monkeypatch):
        from app.routes import reminders
        monkeypatch.setenv("XIAOZHI_OTA_DEVICE_ENABLE", "1")
        offer = reminders._ota_device_offer()
        assert offer is not None
        assert "auto_listen" in offer
        assert "ota_url" not in offer

    def test_included_when_set(self, monkeypatch):
        from app.routes import reminders
        from app.runtime_settings import set_value
        monkeypatch.setenv("XIAOZHI_OTA_DEVICE_ENABLE", "1")
        set_value("device.ota_url", "http://192.168.1.77:8000/xiaozhi/ota")
        offer = reminders._ota_device_offer()
        assert offer["ota_url"] == "http://192.168.1.77:8000/xiaozhi/ota"

    def test_omitted_when_empty_string(self, monkeypatch):
        # 显式清空 = 覆盖里存空串：仍然不能下发，否则会把设备导向空地址
        from app.routes import reminders
        from app.runtime_settings import set_value
        monkeypatch.setenv("XIAOZHI_OTA_DEVICE_ENABLE", "1")
        set_value("device.ota_url", "")
        offer = reminders._ota_device_offer()
        assert "ota_url" not in offer

    def test_disabled_returns_none(self, monkeypatch):
        from app.routes import reminders
        monkeypatch.setenv("XIAOZHI_OTA_DEVICE_ENABLE", "0")
        assert reminders._ota_device_offer() is None
