"""Tab titles: a chat must not stay called "New Chat" once it has a prompt.

With several sessions open, identical titles make the tab bar unusable, so the
summariser failing (offline, no credits, backend hiccup) must still leave a
tab named after what was asked.
"""

import pytest


@pytest.fixture(scope="module")
def chat_ui():
    from windows import ai_chat_ui
    return ai_chat_ui


def test_a_short_prompt_becomes_the_title_verbatim(chat_ui):
    assert chat_ui._short_title("trim the intro") == "trim the intro"


def test_a_long_prompt_is_cut_to_a_few_words(chat_ui):
    title = chat_ui._short_title(
        "please trim the intro and then colour grade the whole second half",
        max_words=6,
    )
    assert title == "please trim the intro and then"
    assert len(title.split()) <= 6


def test_newlines_and_padding_collapse(chat_ui):
    assert chat_ui._short_title("  trim\n\n  the   intro  ") == "trim the intro"


def test_an_empty_prompt_has_no_title(chat_ui):
    assert chat_ui._short_title("   \n ") == ""


def test_the_prompt_is_used_when_the_summariser_fails(chat_ui, monkeypatch):
    """The whole point: a dead backend must not leave every tab "New Chat"."""
    def boom(*a, **kw):
        raise RuntimeError("backend down")

    monkeypatch.setattr(chat_ui, "get_backend_client", boom)
    assert chat_ui._summarize_prompt("trim the intro") == "trim the intro"


def test_the_prompt_is_used_when_the_summariser_returns_nothing(chat_ui, monkeypatch):
    class _Client:
        def auth_token(self):
            return "t"

        def send_message_ws(self, **kw):
            return "   "

    monkeypatch.setattr(chat_ui, "get_backend_client", lambda: _Client())
    assert chat_ui._summarize_prompt("trim the intro") == "trim the intro"


def test_a_real_summary_still_wins(chat_ui, monkeypatch):
    class _Client:
        def auth_token(self):
            return "t"

        def send_message_ws(self, **kw):
            return "Trim intro"

    monkeypatch.setattr(chat_ui, "get_backend_client", lambda: _Client())
    assert chat_ui._summarize_prompt("trim the intro please") == "Trim intro"
