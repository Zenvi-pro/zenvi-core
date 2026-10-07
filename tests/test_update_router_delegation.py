"""UpdatesRouter must forward state writes to the real UpdateManager.

Regression test for the bug that made every transaction a no-op:

``app.updates`` is an ``UpdatesRouter``, which delegates *reads* through
``__getattr__`` but defined no ``__setattr__``.  So every

    app.updates.transaction_id = tid

(~20 call sites, agent and GUI alike) created an attribute on the *router*
while ``UpdateManager.transaction_id`` stayed ``None`` forever.  Since
``UpdateAction.__init__`` mints a fresh uuid4 whenever the manager's id is
unset, each mutation landed in its own transaction and ``undo()`` -- which
groups by transaction id -- reversed exactly one of them.

Symptom: "undo my last change" reported success while the clip stayed on the
timeline, because one composite handler had produced four undo steps.
"""

from classes.update_queue import UpdateQueue, UpdatesRouter
from classes.updates import UpdateManager


def _router():
    manager = UpdateManager()
    return manager, UpdatesRouter(manager, UpdateQueue(manager))


# --------------------------------------------------------------------------
# The delegation contract
# --------------------------------------------------------------------------

def test_transaction_id_write_reaches_the_manager():
    manager, router = _router()
    router.transaction_id = "TXN-1"
    assert manager.transaction_id == "TXN-1", (
        "the write stopped at the router; UpdateManager never sees the "
        "transaction id, so every mutation gets its own undo step"
    )


def test_transaction_id_read_reflects_the_manager():
    manager, router = _router()
    manager.transaction_id = "TXN-2"
    assert router.transaction_id == "TXN-2"


def test_transaction_id_clears_through_the_router():
    manager, router = _router()
    router.transaction_id = "TXN-3"
    router.transaction_id = None
    assert manager.transaction_id is None


def test_ignore_history_write_reaches_the_manager():
    manager, router = _router()
    router.ignore_history = True
    assert manager.ignore_history is True, (
        "a router-local ignore_history silently fails to suppress history"
    )
    router.ignore_history = False
    assert manager.ignore_history is False


def test_pending_and_last_action_write_through():
    manager, router = _router()
    router.pending_action = "P"
    router.last_action = "L"
    assert manager.pending_action == "P"
    assert manager.last_action == "L"


def test_router_does_not_shadow_forwarded_state():
    """Nothing forwarded may live in the router's own __dict__."""
    _manager, router = _router()
    router.transaction_id = "TXN-4"
    router.ignore_history = True
    leaked = set(router.__dict__) - {"_updates", "_queue"}
    assert not leaked, f"router shadows manager state: {sorted(leaked)}"


# --------------------------------------------------------------------------
# What the delegation is actually for: grouping
# --------------------------------------------------------------------------

def test_mutations_through_the_router_share_one_transaction():
    manager, router = _router()
    router.transaction_id = "TXN-5"
    router.insert(["clips"], {"id": "a"})
    router.update(["clips", {"id": "a"}], {"position": 1.0})
    router.transaction_id = None

    assert len(manager.actionHistory) == 2
    assert {a.transaction for a in manager.actionHistory} == {"TXN-5"}, (
        "two mutations for one user action must land in ONE undo step"
    )


def test_ungrouped_mutations_get_one_transaction_each():
    """The pre-fix behaviour, kept as a contrast case."""
    manager, router = _router()
    router.insert(["clips"], {"id": "a"})
    router.update(["clips", {"id": "a"}], {"position": 1.0})
    assert len({a.transaction for a in manager.actionHistory}) == 2


def test_ignore_history_through_the_router_suppresses_history():
    manager, router = _router()
    router.ignore_history = True
    router.insert(["clips"], {"id": "a"})
    assert manager.actionHistory == []
    assert manager.pending_action is not None
