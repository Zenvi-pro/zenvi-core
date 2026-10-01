"""Fractional display scales (Windows 125%) must round, not pass through.

PassThrough renders the whole editor at 1.25x with soft, pixelated icons.
"""
import ast
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY = "QT_SCALE_FACTOR_ROUNDING_POLICY"


def _launch_tree():
    with open(os.path.join(REPO_ROOT, "src", "launch.py"), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _policy_writes(tree):
    """Every statement in launch.py that names the rounding-policy env var."""
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.Expr))
        and any(isinstance(c, ast.Constant) and c.value == POLICY for c in ast.walk(node))
    ]


def test_launch_rounds_fractional_display_scale_unless_the_user_chose():
    writes = _policy_writes(_launch_tree())
    assert len(writes) == 1
    call = writes[0].value
    # os.environ.setdefault(POLICY, "Round"): an exported policy is not overridden.
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    assert call.func.attr == "setdefault"
    assert ast.unparse(call.func.value) == "os.environ"
    assert [a.value for a in call.args] == [POLICY, "Round"]


def test_policy_is_set_before_the_application_is_constructed():
    """Qt reads the variable when QApplication (OpenShotApp) is created."""
    tree = _launch_tree()
    (write,) = _policy_writes(tree)
    app_lines = [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "OpenShotApp"
    ]
    assert app_lines and write.lineno < min(app_lines)
