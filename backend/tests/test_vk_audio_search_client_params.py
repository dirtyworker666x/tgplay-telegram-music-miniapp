"""Unit tests for VK audio.search client params (https, device_id, execute injection)."""
import os

import pytest

import server_lite


@pytest.fixture
def sample_state():
    return server_lite._TokenState("my_test_token_xyz", None, None, "VKAndroidApp/8.17-test")


def test_vk_device_id_from_env(monkeypatch, sample_state):
    monkeypatch.setenv("VK_DEVICE_ID", "aabbccddeeff00112233445566778899")
    monkeypatch.delenv("VK_DEVICE_ID_RANDOM", raising=False)
    assert server_lite._vk_device_id_for_state(sample_state) == "aabbccddeeff00112233445566778899"


def test_vk_device_id_stable_from_token(monkeypatch, sample_state):
    monkeypatch.delenv("VK_DEVICE_ID", raising=False)
    monkeypatch.delenv("VK_DEVICE_ID_RANDOM", raising=False)
    d1 = server_lite._vk_device_id_for_state(sample_state)
    d2 = server_lite._vk_device_id_for_state(sample_state)
    assert d1 == d2
    assert len(d1) == 32
    assert all(c in "0123456789abcdef" for c in d1)


def test_vk_audio_search_extra_params(monkeypatch, sample_state):
    monkeypatch.delenv("VK_DEVICE_ID", raising=False)
    monkeypatch.delenv("VK_DEVICE_ID_RANDOM", raising=False)
    extra = server_lite._vk_audio_search_extra_params(sample_state)
    assert extra["https"] == 1
    assert len(extra["device_id"]) == 32


def test_vk_expand_audio_search(monkeypatch, sample_state):
    monkeypatch.delenv("VK_DEVICE_ID", raising=False)
    monkeypatch.delenv("VK_DEVICE_ID_RANDOM", raising=False)
    out = server_lite._vk_expand_vk_method_params(
        "audio.search", {"q": "test", "count": 5}, sample_state
    )
    assert out["q"] == "test"
    assert out["count"] == 5
    assert out["https"] == 1
    assert "device_id" in out


def test_vk_expand_audio_getbyid(monkeypatch, sample_state):
    monkeypatch.delenv("VK_DEVICE_ID", raising=False)
    monkeypatch.delenv("VK_DEVICE_ID_RANDOM", raising=False)
    out = server_lite._vk_expand_vk_method_params(
        "audio.getById", {"audios": "1_2,3_4"}, sample_state
    )
    assert out["audios"] == "1_2,3_4"
    assert out["https"] == 1
    assert "device_id" in out


def test_vk_execute_inject_audio_search_extras(monkeypatch, sample_state):
    monkeypatch.setenv("VK_DEVICE_ID", "deadbeef")
    code = (
        'var v0=API.audio.search({"q":"hello","count":50,"sort":0,"auto_complete":0,"search_own":0});'
        'return {"v0":v0.items};'
    )
    injected = server_lite._vk_execute_inject_audio_search_extras(code, sample_state)
    assert '"https":1' in injected
    assert '"device_id":"deadbeef"' in injected
    assert 'API.audio.search({"q":' not in injected


def test_vk_user_agents_json_parsing(monkeypatch):
    monkeypatch.delenv("VK_USER_AGENTS_JSON", raising=False)
    monkeypatch.delenv("VK_USER_AGENTS", raising=False)
    monkeypatch.setenv(
        "VK_USER_AGENTS_JSON",
        '["KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86, comma inside; en)", "VKAndroidApp/8.17-test"]',
    )
    lst = server_lite._vk_user_agents_list_from_env()
    assert len(lst) == 2
    assert "comma inside" in lst[0]
    assert lst[1] == "VKAndroidApp/8.17-test"


def test_vk_user_agents_triple_pipe_split(monkeypatch):
    monkeypatch.delenv("VK_USER_AGENTS_JSON", raising=False)
    a = "KateMobileAndroid/56 (Android 11; SDK 30; x86, y; ru)"
    b = "VKAndroidApp/8.17 (Android 12; ok, ok; en)"
    monkeypatch.setenv("VK_USER_AGENTS", f"{a}|||{b}")
    lst = server_lite._vk_user_agents_list_from_env()
    assert lst == [a, b]


def test_vk_pick_user_agent_single_applies_to_all_indices():
    uas = ["OnlyKateUA/1"]
    assert server_lite._vk_pick_user_agent_for_token(uas, 0) == "OnlyKateUA/1"
    assert server_lite._vk_pick_user_agent_for_token(uas, 5) == "OnlyKateUA/1"


def test_vk_pick_user_agent_per_token_index():
    uas = ["UA0", "UA1"]
    assert server_lite._vk_pick_user_agent_for_token(uas, 0) == "UA0"
    assert server_lite._vk_pick_user_agent_for_token(uas, 1) == "UA1"
    # третий токен — ротация дефолтных Kate UA
    assert "KateMobileAndroid" in server_lite._vk_pick_user_agent_for_token(uas, 2)


def test_vk_api_client_headers():
    h = server_lite._vk_api_client_headers("VKAndroidApp/8.17 (test)")
    assert h["User-Agent"] == "VKAndroidApp/8.17 (test)"
    if server_lite.VK_X_VK_ANDROID_CLIENT:
        assert h.get("X-VK-Android-Client") == server_lite.VK_X_VK_ANDROID_CLIENT


def test_vk_worker_client_merge_audio_search(monkeypatch):
    monkeypatch.setenv("VK_TOKEN", "worker_token_abc")
    monkeypatch.delenv("VK_DEVICE_ID", raising=False)
    monkeypatch.delenv("VK_DEVICE_ID_RANDOM", raising=False)
    from vk_worker.vk_client import _merge_audio_search_and_execute

    out = _merge_audio_search_and_execute("audio.search", {"q": "x", "count": 2}, "worker_token_abc")
    assert out["https"] == 1
    assert len(out["device_id"]) == 32


def test_vk_worker_client_merge_execute(monkeypatch):
    monkeypatch.setenv("VK_TOKEN", "t")
    monkeypatch.setenv("VK_DEVICE_ID", "abc")
    from vk_worker.vk_client import _merge_audio_search_and_execute

    code = 'var v0=API.audio.search({"q":"z","count":1,"sort":0,"auto_complete":0,"search_own":0});return {"v0":v0.items};'
    out = _merge_audio_search_and_execute("execute", {"code": code}, "t")
    assert '"device_id":"abc"' in out["code"]


def test_vk_worker_client_merge_execute_getbyid(monkeypatch):
    monkeypatch.setenv("VK_TOKEN", "t")
    monkeypatch.setenv("VK_DEVICE_ID", "abc")
    from vk_worker.vk_client import _merge_audio_search_and_execute

    code = 'return API.audio.getById({"audios":"1_2,3_4"});'
    out = _merge_audio_search_and_execute("execute", {"code": code}, "t")
    assert 'API.audio.getById({"https":1' in out["code"]
    assert '"device_id":"abc"' in out["code"]
