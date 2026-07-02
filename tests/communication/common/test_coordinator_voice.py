from common.coordinator_voice import (
    COORDINATOR_DEFAULT_VOICE_ID,
    COORDINATOR_DEFAULT_VOICE_PROVIDER,
    resolve_runtime_voice,
)


def test_resolve_runtime_voice_uses_coordinator_default_when_missing():
    provider, voice_id = resolve_runtime_voice(
        is_coordinator=True,
        voice_provider=None,
        voice_id=None,
    )
    assert provider == COORDINATOR_DEFAULT_VOICE_PROVIDER
    assert voice_id == COORDINATOR_DEFAULT_VOICE_ID


def test_resolve_runtime_voice_keeps_explicit_coordinator_voice():
    provider, voice_id = resolve_runtime_voice(
        is_coordinator=True,
        voice_provider="elevenlabs",
        voice_id="custom-voice",
    )
    assert provider == "elevenlabs"
    assert voice_id == "custom-voice"


def test_resolve_runtime_voice_uses_cartesia_default_for_hired_assistant():
    provider, voice_id = resolve_runtime_voice(
        is_coordinator=False,
        voice_provider=None,
        voice_id=None,
    )
    assert provider == "cartesia"
    assert voice_id == ""
