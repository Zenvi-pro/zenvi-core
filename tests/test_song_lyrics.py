"""Song lyrics: typed words are kept; otherwise the indexing model writes them and credits are charged."""

from classes import song_lyrics as sl


def test_a_missing_route_and_an_empty_google_balance_are_explained():
    missing = sl.explain_lyrics_error("Not Found")
    assert "lyrics route" in missing
    empty = sl.explain_lyrics_error("402 RESOURCE_EXHAUSTED. prepayment credits are depleted")
    assert "Google prepay credits" in empty


def test_typed_lyrics_skip_the_model(monkeypatch):
    def boom(_description):
        raise AssertionError("the indexing model should not run")

    monkeypatch.setattr(sl, "write_lyrics", boom)
    assert sl.prepare_lyrics("rain on a piano", "[Chorus]\nhello") == "[Chorus]\nhello"


def test_a_description_is_written_by_the_indexing_model_and_charged(monkeypatch):
    charged = {}

    monkeypatch.setattr(sl, "check_operation", lambda *args, **kwargs: (True, 12, None))

    def charge(success, operation, **kwargs):
        charged["success"] = success
        charged["operation"] = operation
        charged.update(kwargs)

    monkeypatch.setattr(sl, "charge_operation_on_success", charge)

    class Client:
        def write_song_lyrics(self, description):
            assert description == "rain on a piano"
            return {
                "lyrics": "[Verse]\nRain on the keys",
                "model": "openai/gpt-6-luna",
                "provider": "openai",
            }

    monkeypatch.setattr(sl, "get_backend_client", lambda: Client())
    assert sl.prepare_lyrics("rain on a piano", "") == "[Verse]\nRain on the keys"
    assert charged["operation"] == "indexing_per_minute"
    assert charged["provider"] == "openai"
    assert "gpt-6-luna" in charged["note"]


def test_a_credit_block_does_not_call_the_model(monkeypatch):
    monkeypatch.setattr(
        sl, "check_operation",
        lambda *args, **kwargs: (False, 0, "Insufficient credits (0 remaining, need 8 for song lyrics)."),
    )

    def boom():
        raise AssertionError("backend should not be called")

    monkeypatch.setattr(sl, "get_backend_client", boom)
    try:
        sl.prepare_lyrics("rain", "")
    except sl.LyricsError as exc:
        assert "credits" in str(exc).lower()
    else:
        raise AssertionError("expected a credit block")
