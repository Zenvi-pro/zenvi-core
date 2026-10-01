"""scripts/bugbot_trigger.py (#159): which open PRs get a "bugbot run" comment.

Everything runs against FakeGitHub, which serves canned REST responses by path and
records every call and POST; the HTTP client is tested with a fake urlopen. No network.
"""
import importlib.util
import io
import json
import os
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

_PATH = os.path.join(os.path.dirname(__file__), "..", "scripts", "bugbot_trigger.py")
_spec = importlib.util.spec_from_file_location("bugbot_trigger", _PATH)
bt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bt)

REPO = "Zenvi-pro/zenvi-core"
ME = "jashanpratapsingh"
BUGBOT = "cursor[bot]"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
# Verbatim from cursor[bot] on #102 and #158.
NOT_ENABLED = ("<!-- BUGBOT_FREE_TIER_DISABLED_UPSELL -->\nBugbot is not enabled for your account, so this "
               "pull request was not reviewed.\n\nEnable Bugbot in the [Cursor dashboard]"
               "(https://www.cursor.com/dashboard/bugbot) to get automatic reviews on future PRs.")
MISMATCH = ("<h3>Bugbot couldn't run — GitHub account mismatch</h3>\n\nThe GitHub account linked to your "
            "Cursor account does not match the PR author.")


def ago(minutes):
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def comment(author, body, minutes_ago, issue=None):
    c = {"user": {"login": author}, "body": body, "created_at": ago(minutes_ago)}
    if issue is not None:
        c["issue_url"] = "https://api.github.com/repos/%s/issues/%d" % (REPO, issue)
    return c


class FakeGitHub:
    """The REST paths bugbot_trigger uses, answered from canned data."""

    def __init__(self, me=ME):
        self.routes = {"/user": {"login": me} if me else bt.GitHubError("GET /user: HTTP 403", 403)}
        self.pulls = []
        self.activity = {}        # ref -> events (or an exception to raise)
        self.pr_comments = []     # every PR's comments, for the repo-wide listing
        self.other_comments = []  # comments on PRs that are no longer open
        self.calls = []
        self.posts = []

    def add_pr(self, number, author=ME, draft=False, pushed=60, opened=600, committed=None,
               reviews=(), comments=(), checks=()):
        """A PR whose head was pushed `pushed` minutes ago (None: no push record)."""
        sha, ref = "%040x" % number, "branch-%d" % number
        self.pulls.append({"number": number, "title": "PR %d" % number, "draft": draft,
                           "user": {"login": author}, "created_at": ago(opened),
                           "head": {"sha": sha, "ref": ref, "repo": {"full_name": REPO}}})
        comments = [dict(c, issue_url="https://api.github.com/repos/%s/issues/%d" % (REPO, number))
                    for c in comments]
        self.pr_comments += comments
        self.routes["/repos/%s/pulls/%d/reviews" % (REPO, number)] = list(reviews)
        self.routes["/repos/%s/issues/%d/comments" % (REPO, number)] = comments
        self.routes["/repos/%s/commits/%s/check-runs" % (REPO, sha)] = {"check_runs": list(checks)}
        if pushed is not None:  # newest first, like the API
            self.activity["refs/heads/" + ref] = [
                {"after": sha, "timestamp": ago(pushed), "activity_type": "push"},
                {"after": "e" * 40, "timestamp": ago(pushed + 30), "activity_type": "branch_creation"},
            ]
        date = committed if committed is not None else (pushed or 0) + 5
        self.routes["/repos/%s/commits/%s" % (REPO, sha)] = {"commit": {"committer": {"date": ago(date)}}}
        return self.pulls[-1]

    def get(self, path, params=None):
        self.calls.append(path)
        if path == "/repos/%s/pulls" % REPO:
            assert params["state"] == "open"
            return self.pulls
        if path == "/repos/%s/issues/comments" % REPO:  # GitHub filters `since` on updated_at
            return [c for c in self.pr_comments + self.other_comments if c["created_at"] >= params["since"]]
        value = self.activity.get(params["ref"], []) if path == "/repos/%s/activity" % REPO else self.routes[path]
        if isinstance(value, Exception):
            raise value
        return value

    get_all = get

    def post(self, path, body):
        self.posts.append((path, body))
        return {"id": len(self.posts)}


def run(api, authors=(ME,), **kwargs):
    lines = []
    due = bt.run(api, REPO, None if authors is None else set(authors), now=NOW, out=lines.append, **kwargs)
    return due, "\n".join(lines)


def row(output, number):
    return next(line for line in output.splitlines() if line.lstrip().startswith("#%d " % number))


def test_comments_bugbot_run_on_an_unreviewed_pr():
    api = FakeGitHub()
    api.add_pr(216)
    due, out = run(api)
    assert due == [216]
    assert api.posts == [("/repos/%s/issues/216/comments" % REPO, {"body": "bugbot run"})]
    assert "commented" in row(out, 216) and "pushed 2026-09-30 11:00 UTC" in row(out, 216)


def test_dry_run_posts_nothing():
    api = FakeGitHub()
    api.add_pr(216)
    due, out = run(api, dry_run=True)
    assert due == [216] and api.posts == []
    assert "would comment" in row(out, 216)
    assert out.splitlines()[-1] == "Would comment 'bugbot run' on 1 PR(s): #216."


def test_drafts_and_other_authors_cost_no_per_pr_calls():
    api = FakeGitHub()
    api.add_pr(198, author="Harkit2004", draft=True)
    api.add_pr(217, draft=True)
    api.add_pr(220, author="nilay-goyal")
    due, out = run(api)
    assert due == [] and api.posts == []
    assert "draft" in row(out, 198) and "draft" in row(out, 217)
    assert "not in --only-authors" in row(out, 220)
    assert set(api.calls) == {"/user", "/repos/%s/issues/comments" % REPO, "/repos/%s/pulls" % REPO}


def test_author_logins_match_case_insensitively_and_all_authors_covers_everyone():
    api = FakeGitHub()
    api.add_pr(216, author="JashanPratapSingh")
    api.add_pr(220, author="nilay-goyal")
    assert run(api, dry_run=True)[0] == [216]
    assert run(api, authors=None, dry_run=True)[0] == [220, 216]


def test_a_pr_bugbot_reviewed_at_any_commit_is_left_alone():
    api = FakeGitHub()
    api.add_pr(7, pushed=10, reviews=[{"user": {"login": "coderabbitai[bot]"}, "commit_id": "a" * 40},
                                      {"user": {"login": BUGBOT}, "commit_id": "64adbb80b" + "0" * 31}])
    due, out = run(api)
    assert due == [] and "already reviewed by Bugbot (commit 64adbb8)" in row(out, 7)


@pytest.mark.parametrize("status, conclusion", [("in_progress", None), ("completed", "neutral")])
def test_a_bugbot_check_on_the_head_means_it_is_already_on_it(status, conclusion):
    api = FakeGitHub()
    api.add_pr(216, checks=[{"name": "build", "app": {"slug": "github-actions"}, "status": "completed"},
                            {"name": "Cursor Bugbot", "app": {"slug": "cursor"}, "status": status,
                             "conclusion": conclusion}])
    due, out = run(api)
    assert due == [] and "Bugbot check 'Cursor Bugbot' is %s" % (conclusion or status) in row(out, 216)


@pytest.mark.parametrize("body", ["bugbot run", "Cursor review verbose=true"])
@pytest.mark.parametrize("pushed, asked, expected", [
    (60, 30, []),     # asked since the push: Bugbot is on it, or ignoring us
    (20, 30, [216]),  # pushed since the ask: a new head, ask again
])
def test_one_ask_per_head(body, pushed, asked, expected):
    api = FakeGitHub()
    api.add_pr(216, pushed=pushed, comments=[comment("Harkit2004", body, asked)])
    due, out = run(api)
    assert due == expected
    if not expected:
        assert "@Harkit2004 asked at 2026-09-30 11:30 UTC" in row(out, 216)


def test_bugbot_answer_since_the_push_is_respected_until_retry():
    api = FakeGitHub()
    api.add_pr(216, pushed=60, comments=[comment(BUGBOT, NOT_ENABLED, 120),  # an older head: irrelevant
                                         comment("Harkit2004", "bugbot run", 30),
                                         comment(BUGBOT, MISMATCH, 29.9)])
    due, out = run(api)
    assert due == []
    assert "Bugbot already answered for head" in row(out, 216)
    assert "Bugbot couldn't run — GitHub account mismatch" in row(out, 216)
    assert run(api, retry=True)[0] == [216]


def test_retry_still_waits_for_an_unanswered_ask():
    api = FakeGitHub()
    api.add_pr(216, pushed=60, comments=[comment(ME, "bugbot run", 30)])
    assert run(api, retry=True)[0] == []


def test_a_refusal_of_our_trigger_holds_every_pr_until_retry():
    api = FakeGitHub()
    api.add_pr(216)
    api.other_comments += [comment(ME, "bugbot run", 120, issue=210), comment(BUGBOT, NOT_ENABLED, 119.9, issue=210)]
    due, out = run(api, annotate=True)
    assert due == [] and api.posts == []
    assert ("::warning::Bugbot refused @jashanpratapsingh's trigger on #210 at 2026-09-30 10:00 UTC: "
            "'Bugbot is not enabled for your account, so this pull request was not reviewed.'") in out
    assert "Posting nothing until 2026-10-01 10:00 UTC" in out
    assert "held" in row(out, 216)
    assert out.splitlines()[-1] == "Commented 'bugbot run' on 0 PR(s); held 1."
    assert run(api, retry=True)[0] == [216]


def test_refusals_of_other_peoples_triggers_or_older_than_the_cooldown_do_not_hold():
    api = FakeGitHub()
    api.add_pr(216)
    api.other_comments += [
        comment("Harkit2004", "bugbot run", 120, issue=158), comment(BUGBOT, MISMATCH, 119.9, issue=158),
        # Fetched (the listing reaches 15 min past the cooldown, to see the trigger a refusal answers) but too old.
        comment(ME, "bugbot run", 24 * 60 + 10, issue=100), comment(BUGBOT, NOT_ENABLED, 24 * 60 + 9.9, issue=100),
    ]
    assert run(api)[0] == [216]


def test_without_knowing_the_token_owner_any_refused_trigger_holds():
    api = FakeGitHub(me=None)
    api.add_pr(216)
    api.other_comments += [comment("Harkit2004", "bugbot run", 120, issue=158), comment(BUGBOT, MISMATCH, 119.9, issue=158)]
    due, out = run(api)
    assert due == [] and "treating every trigger as ours" in out


def test_fresh_pushes_and_new_prs_are_left_to_bugbots_own_review():
    api = FakeGitHub()
    api.add_pr(216, pushed=1)
    api.add_pr(217, pushed=600, opened=1)
    due, out = run(api)
    assert due == []
    assert "60s ago; Bugbot's own automatic review goes first" in row(out, 216)
    assert "60s ago" in row(out, 217)
    assert run(api, grace=timedelta(0))[0] == [217, 216]


@pytest.mark.parametrize("fork_gone", [False, True])
@pytest.mark.parametrize("committed, expected", [(60, []), (10, [216])])
def test_push_time_falls_back_to_the_commit_date(fork_gone, committed, expected):
    api = FakeGitHub()
    pr = api.add_pr(216, pushed=None, committed=committed, comments=[comment(ME, "bugbot run", 30)])
    if fork_gone:
        pr["head"]["repo"] = None
    due, out = run(api)
    assert due == expected
    if expected:
        assert "committed 2026-09-30 11:50 UTC (no push record)" in row(out, 216)
    else:
        assert "not asking twice for head" in row(out, 216)
    assert ("/repos/%s/activity" % REPO in api.calls) is not fork_gone


def test_push_time_is_the_heads_own_push_even_after_the_branch_moved_on():
    # The PR listing can lag a push by a moment: the newest activity is then a SHA the PR does not show yet.
    api = FakeGitHub()
    api.add_pr(216, pushed=60, comments=[comment(ME, "bugbot run", 45)])
    api.activity["refs/heads/branch-216"].insert(0, {"after": "d" * 40, "timestamp": ago(30), "activity_type": "push"})
    due, out = run(api)
    assert due == [] and "not asking twice for head" in row(out, 216)


def test_a_future_commit_date_makes_every_earlier_ask_count():
    api = FakeGitHub()
    api.add_pr(216, pushed=None, committed=-120, comments=[comment(ME, "bugbot run", 3 * 24 * 60)])
    due, out = run(api)
    assert due == [] and "not asking twice for this PR (push time unknown)" in row(out, 216)
    api = FakeGitHub()
    api.add_pr(216, pushed=None, committed=-120)
    assert run(api)[0] == [216]


def test_activity_404_falls_back_but_other_errors_stop_the_run():
    api = FakeGitHub()
    api.add_pr(216)
    api.activity["refs/heads/branch-216"] = bt.GitHubError("GET activity: HTTP 404", 404)
    due, out = run(api)
    assert due == [216] and "(no push record)" in row(out, 216)
    api.activity["refs/heads/branch-216"] = bt.GitHubError("GET activity: HTTP 502", 502)
    with pytest.raises(bt.GitHubError):
        run(api)


def test_max_comments_caps_one_run():
    api = FakeGitHub()
    for number in (216, 217, 218):
        api.add_pr(number)
    due, out = run(api, max_comments=2)
    assert due == [218, 217] and len(api.posts) == 2
    assert "held: --max-comments reached" in row(out, 216)


@pytest.mark.parametrize("author, body, expected", [
    (ME, "bugbot run", True),
    (ME, "  Bugbot Run verbose=true", True),
    (ME, "cursor review", True),
    (ME, "please bugbot run", False),
    (ME, "bugbot running?", False),
    (BUGBOT, "Bugbot run skipped", False),
])
def test_what_counts_as_a_trigger(author, body, expected):
    assert bt.is_trigger(comment(author, body, 0)) is expected


def test_first_line_drops_bugbots_html_markers():
    assert bt.first_line(NOT_ENABLED) == "Bugbot is not enabled for your account, so this pull request was not reviewed."
    assert bt.first_line(MISMATCH) == "Bugbot couldn't run — GitHub account mismatch"
    assert bt.is_refusal(comment(BUGBOT, MISMATCH, 0)) and not bt.is_refusal(comment(ME, MISMATCH, 0))


class _Response:
    def __init__(self, payload, link=None):
        self._body = json.dumps(payload).encode()
        self.headers = {"Link": link} if link else {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_client_follows_pages_and_keeps_the_token_in_a_header():
    page2 = "https://api.github.com/repositories/1/pulls?state=open&per_page=100&page=2"
    responses = {
        "https://api.github.com/repos/o/r/pulls?state=open&per_page=100":
            _Response([{"number": 2}], '<%s>; rel="next", <%s>; rel="last"' % (page2, page2)),
        page2: _Response([{"number": 1}], '<https://api.github.com/repositories/1/pulls?page=1>; rel="prev"'),
    }
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        return responses[request.full_url]

    gh = bt.GitHub("secret-token", urlopen=urlopen)
    assert gh.get_all("/repos/o/r/pulls", {"state": "open", "per_page": 100}) == [{"number": 2}, {"number": 1}]
    assert [r.get_header("Authorization") for r in seen] == ["Bearer secret-token"] * 2
    assert not any("secret-token" in r.full_url for r in seen)


def test_client_posts_json_and_encodes_query_values():
    seen = []

    def urlopen(request, timeout):
        seen.append(request)
        return _Response({"id": 5})

    gh = bt.GitHub("t", urlopen=urlopen)
    assert gh.post("/repos/o/r/issues/216/comments", {"body": "bugbot run"}) == {"id": 5}
    assert seen[0].get_method() == "POST" and json.loads(seen[0].data) == {"body": "bugbot run"}
    assert seen[0].get_header("Content-type") == "application/json"
    # Branch names come from PR authors; they must not be able to add query parameters.
    assert gh.url("/repos/o/r/activity", {"ref": "refs/heads/a&per_page=1#x"}) == (
        "https://api.github.com/repos/o/r/activity?ref=refs%2Fheads%2Fa%26per_page%3D1%23x")


def test_client_errors_carry_the_http_status():
    def urlopen(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message": "Not Found"}'))

    with pytest.raises(bt.GitHubError) as info:
        bt.GitHub("t", urlopen=urlopen).get("/repos/o/r/activity")
    assert info.value.status == 404 and "Not Found" in str(info.value)


def test_main_needs_a_token(monkeypatch, capsys):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(bt, "gh_cli_token", lambda: None)
    assert bt.main(["--dry-run"]) == 2
    assert "no GitHub token" in capsys.readouterr().err


def test_main_passes_options_through_and_reports_api_errors(monkeypatch, capsys):
    monkeypatch.setenv("GH_TOKEN", "t")
    seen = {}
    monkeypatch.setattr(bt, "run", lambda gh, repo, authors, **kw: seen.update(repo=repo, authors=authors, **kw))
    assert bt.main(["--repo", "o/r", "--only-authors", "Alice, bob", "--dry-run", "--max-comments", "3"]) == 0
    assert (seen["repo"], seen["authors"], seen["dry_run"], seen["max_comments"]) == ("o/r", {"alice", "bob"}, True, 3)
    assert bt.main(["--all-authors", "--retry"]) == 0
    assert seen["authors"] is None and seen["retry"] is True

    def broken(*args, **kwargs):
        raise bt.GitHubError("GET /repos/o/r/pulls: HTTP 502 bad gateway", 502)

    monkeypatch.setattr(bt, "run", broken)
    assert bt.main([]) == 1
    assert "HTTP 502" in capsys.readouterr().err
