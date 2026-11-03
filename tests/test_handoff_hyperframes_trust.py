"""Session trust for UI-initiated HyperFrames renders / Studio."""

from classes.handoff.hyperframes import trust


def test_require_skips_agent_threads_and_remembers_ui_yes(monkeypatch):
    trust.reset()
    root = "/tmp/hf-proj"
    # agent / tool threads are not gated
    monkeypatch.setattr(trust, "ui_initiated", lambda: False)
    trust.require(root, "proj", action="render")  # no asker needed
    monkeypatch.setattr(trust, "ui_initiated", lambda: True)
    asks = []

    def asker(r, name, action, key):
        asks.append((r, name, action, key))
        return True

    trust.set_asker(asker)
    try:
        trust.require(root, "proj", action="render")
        assert len(asks) == 1 and asks[0][2] == "render"
        trust.require(root, "proj", action="studio")  # already trusted this session
        assert len(asks) == 1
    finally:
        trust.set_asker(None)
        trust.reset()


def test_require_no_raises_not_trusted(monkeypatch):
    trust.reset()
    monkeypatch.setattr(trust, "ui_initiated", lambda: True)
    trust.set_asker(lambda *a: False)
    try:
        try:
            trust.require("/tmp/nope", "nope", action="render")
            assert False, "expected NotTrusted"
        except trust.NotTrusted:
            pass
    finally:
        trust.set_asker(None)
        trust.reset()
