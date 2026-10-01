#!/usr/bin/env python3
"""Comment "bugbot run" on open PRs that Cursor Bugbot has not reviewed yet (#159).

Bugbot reviews a PR when someone comments ``bugbot run`` from a GitHub account
linked to a Cursor account that has Bugbot. On an individual Cursor plan it only
reviews PRs that account authored -- anyone else's PR gets "GitHub account
mismatch" (#158) -- so only PRs by --only-authors are considered. --all-authors
is for a Cursor team that covers the repository.

An open, non-draft PR by a covered author gets the comment unless:
  * cursor[bot] has already reviewed it (any commit);
  * Bugbot has a check run on the current head (running, or done with nothing to say);
  * its head was pushed, or it was opened, under --grace-minutes ago: Bugbot's own
    automatic review goes first;
  * someone asked (``bugbot run`` / ``cursor review``) since the head was pushed
    and Bugbot has not answered: it is working, or ignoring us;
  * Bugbot answered since the head was pushed ("not enabled for your account",
    "used all of your free Bugbot PR reviews", ...): same head, same answer.
Nothing is posted anywhere while Bugbot's answer to one of our own triggers in the
last --cooldown-hours was a refusal: every new trigger would earn another one.
--retry ignores refusals, for use once the Cursor side is fixed.

The push time comes from the repository activity API (server side); failing that,
the head commit's committer date; failing that (a date in the future), every
earlier trigger and answer counts as covering the current head.

  bugbot_trigger.py --dry-run                 what it would do, posting nothing
  bugbot_trigger.py --all-authors --dry-run   the same for every author's PRs
The token comes from GH_TOKEN or GITHUB_TOKEN, else `gh auth token`.
.github/workflows/bugbot-trigger.yml runs this hourly and on PR events.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API_URL = "https://api.github.com"
DEFAULT_REPO = "Zenvi-pro/zenvi-core"
DEFAULT_AUTHORS = "jashanpratapsingh"
BUGBOT = "cursor[bot]"
TRIGGER = "bugbot run"

# Bugbot's documented triggers; "bugbot run verbose=true" counts too.
TRIGGER_RE = re.compile(r"\s*(?:bugbot\s+run|cursor\s+review)\b", re.IGNORECASE)
# Phrases from Bugbot's refusals on this repository (#9, #44-#102, #158).
REFUSALS = (
    "not enabled for your account",
    "used all of your free bugbot",
    "unable to authenticate your request",
    "couldn't run",
)
# Bugbot answers within seconds; a refusal belongs to the trigger just before it.
ANSWER_WINDOW = timedelta(minutes=15)


class GitHubError(RuntimeError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class GitHub:
    """The few REST calls this script needs, over urllib."""

    def __init__(self, token, api_url=API_URL, urlopen=urllib.request.urlopen):
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._urlopen = urlopen

    def url(self, path, params=None):
        query = "?" + urllib.parse.urlencode(params) if params else ""
        return self._api_url + path + query

    def _call(self, method, url, body=None):
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer " + self._token,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "zenvi-bugbot-trigger",
        })
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self._urlopen(request, timeout=30) as response:
                payload = response.read()
                link = response.headers.get("Link") or ""
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise GitHubError("%s %s: HTTP %d %s" % (method, url, exc.code, detail), exc.code) from None
        except urllib.error.URLError as exc:
            raise GitHubError("%s %s: %s" % (method, url, exc.reason)) from None
        return (json.loads(payload) if payload else None), link

    def get(self, path, params=None):
        return self._call("GET", self.url(path, params))[0]

    def get_all(self, path, params=None):
        """Every page of a list endpoint, following the Link header."""
        url, items = self.url(path, params), []
        while url:
            page, link = self._call("GET", url)
            items.extend(page or [])
            found = re.search(r'<([^>]+)>;\s*rel="next"', link)
            url = found.group(1) if found else None
        return items

    def post(self, path, body):
        return self._call("POST", self.url(path), body)[0]


def timestamp(value):
    """GitHub's "2026-09-30T21:43:08Z" as an aware datetime (3.10's fromisoformat rejects the Z)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def show(when):
    return when.strftime("%Y-%m-%d %H:%M UTC")


def login(item):
    return ((item or {}).get("user") or {}).get("login") or ""


def created(comment):
    return timestamp(comment["created_at"])


def is_trigger(comment):
    return login(comment) != BUGBOT and bool(TRIGGER_RE.match(comment.get("body") or ""))


def is_refusal(comment):
    body = (comment.get("body") or "").lower()
    return login(comment) == BUGBOT and any(phrase in body for phrase in REFUSALS)


def first_line(body, limit=100):
    """The first readable line of a Bugbot reply, without its HTML markers."""
    text = re.sub(r"<!--.*?-->|<[^>]+>", "", body or "", flags=re.DOTALL)
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[:limit - 3] + "..."


def is_bugbot_check(run):
    return (run.get("app") or {}).get("slug") == "cursor" or "bugbot" in (run.get("name") or "").lower()


def head_pushed_at(gh, repo, pr, now):
    """(when the PR's head commit was pushed, or None if unknown; how we know)."""
    head = pr["head"]
    source = (head.get("repo") or {}).get("full_name")  # None when a fork was deleted
    if source:
        try:
            activity = gh.get("/repos/%s/activity" % source, {"ref": "refs/heads/" + head["ref"], "per_page": 100})
        except GitHubError as exc:
            if exc.status != 404:
                raise
            activity = []
        for event in activity or []:  # newest first
            if event.get("after") == head["sha"] and event.get("timestamp"):
                when = timestamp(event["timestamp"])
                return when, "pushed " + show(when)
    commit = gh.get("/repos/%s/commits/%s" % (repo, head["sha"]))
    date = (((commit or {}).get("commit") or {}).get("committer") or {}).get("date")
    when = timestamp(date) if date else None
    if when and when <= now:
        return when, "committed %s (no push record)" % show(when)
    return None, "push time unknown"


def evaluate(gh, repo, pr, now, authors, grace, retry):
    """(True, why) when the PR should get "bugbot run" now, else (False, why not)."""
    number, author, sha = pr["number"], login(pr), pr["head"]["sha"]
    if pr.get("draft"):
        return False, "draft"
    if authors is not None and author.lower() not in authors:
        return False, "author not in --only-authors"
    reviews = [r for r in gh.get_all("/repos/%s/pulls/%d/reviews" % (repo, number), {"per_page": 100})
               if login(r) == BUGBOT]
    if reviews:
        return False, "already reviewed by Bugbot (commit %s)" % (reviews[-1].get("commit_id") or "?")[:7]
    runs = (gh.get("/repos/%s/commits/%s/check-runs" % (repo, sha), {"per_page": 100}) or {}).get("check_runs", [])
    for run in filter(is_bugbot_check, runs):
        state = run.get("conclusion") or run.get("status")
        return False, "Bugbot check %r is %s on head %s" % (run.get("name"), state, sha[:7])
    pushed, basis = head_pushed_at(gh, repo, pr, now)
    opened = timestamp(pr["created_at"])
    fresh = max(pushed, opened) if pushed else opened
    if now - fresh < grace:
        return False, "head pushed or PR opened %ds ago; Bugbot's own automatic review goes first" % (
            (now - fresh).total_seconds())
    comments = gh.get_all("/repos/%s/issues/%d/comments" % (repo, number), {"per_page": 100})
    recent = [c for c in comments if pushed is None or created(c) > pushed]
    answers = [c for c in recent if login(c) == BUGBOT]
    scope = "head %s" % sha[:7] if pushed else "this PR (%s)" % basis
    if answers and not retry:
        return False, "Bugbot already answered for %s: %r" % (scope, first_line(answers[-1]["body"]))
    for trigger in filter(is_trigger, recent):
        if not any(created(a) >= created(trigger) for a in answers):
            return False, "@%s asked at %s and Bugbot has not answered; not asking twice for %s" % (
                login(trigger), show(created(trigger)), scope)
    return True, "no Bugbot review yet; head %s %s" % (sha[:7], basis)


def recent_refusal(gh, repo, me, now, cooldown):
    """(PR number, refusal, trigger) for Bugbot's latest refusal of a trigger posted by `me`
    since now - cooldown, or None. With `me` unknown, anyone's trigger counts."""
    cutoff = now - cooldown
    comments = gh.get_all("/repos/%s/issues/comments" % repo,
                          {"since": iso(cutoff - ANSWER_WINDOW), "per_page": 100})
    threads = {}
    for comment in comments:
        threads.setdefault(int(comment["issue_url"].rsplit("/", 1)[1]), []).append(comment)
    latest = None
    for number, thread in threads.items():
        trigger = None
        for comment in sorted(thread, key=created):
            if is_trigger(comment):
                trigger = comment
            elif (is_refusal(comment) and trigger is not None and created(comment) >= cutoff
                  and created(comment) - created(trigger) <= ANSWER_WINDOW
                  and (me is None or login(trigger).lower() == me.lower())):
                if latest is None or created(comment) > created(latest[1]):
                    latest = (number, comment, trigger)
    return latest


def run(gh, repo, authors, dry_run=False, retry=False, grace=timedelta(minutes=2),
        cooldown=timedelta(hours=24), max_comments=10, now=None, out=print, annotate=False):
    """Comment on every PR that is due; return their numbers (would-be, in a dry run)."""
    now = now or datetime.now(timezone.utc)
    try:
        me = gh.get("/user")["login"]
    except GitHubError as exc:
        me = None
        out("Could not tell whose token this is (%s); treating every trigger as ours." % exc)
    covering = "every author" if authors is None else ", ".join(sorted(authors))
    out("Bugbot trigger for %s as @%s, PRs by %s%s" % (
        repo, me or "?", covering, " (dry run: nothing is posted)" if dry_run else ""))
    if me and authors is not None and me.lower() not in authors:
        out("Note: @%s is not in --only-authors; Bugbot on an individual plan only reviews PRs by the "
            "account that asks." % me)
    refusal = None if retry else recent_refusal(gh, repo, me, now, cooldown)
    if refusal:
        number, answer, trigger = refusal
        out("%sBugbot refused @%s's trigger on #%d at %s: %r. Posting nothing until %s; fix the Cursor "
            "side, then run with --retry." % ("::warning::" if annotate else "", login(trigger), number,
                                             show(created(answer)), first_line(answer["body"]),
                                             show(created(answer) + cooldown)))
    due, held = [], 0
    pulls = gh.get_all("/repos/%s/pulls" % repo, {"state": "open", "per_page": 100})
    for pr in sorted(pulls, key=lambda p: -p["number"]):
        ok, why = evaluate(gh, repo, pr, now, authors, grace, retry)
        if not ok:
            label = "skip"
        elif refusal or len(due) >= max_comments:
            label, held = "held", held + 1
            why += "; held: Bugbot refused a trigger (above)" if refusal else "; held: --max-comments reached"
        elif dry_run:
            label = "would comment"
            due.append(pr["number"])
        else:
            gh.post("/repos/%s/issues/%d/comments" % (repo, pr["number"]), {"body": TRIGGER})
            label = "commented"
            due.append(pr["number"])
        out("  #%-4d %-13s @%-18s %s | %s" % (pr["number"], label, login(pr), why, pr.get("title", "")[:60]))
    out("%s %r on %d PR(s)%s%s." % ("Would comment" if dry_run else "Commented", TRIGGER, len(due),
                                   ": " + ", ".join("#%d" % n for n in due) if due else "",
                                   "; held %d" % held if held else ""))
    return due


def gh_cli_token():
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO,
                        help="OWNER/NAME (default: $GITHUB_REPOSITORY, else %(default)s)")
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--only-authors", default=DEFAULT_AUTHORS, metavar="LOGIN[,LOGIN...]",
                     help="only PRs by these authors (default: %(default)s)")
    who.add_argument("--all-authors", action="store_true",
                     help="every author's PRs (needs a Cursor team covering the repository)")
    parser.add_argument("--dry-run", action="store_true", help="print what it would do, post nothing")
    parser.add_argument("--retry", action="store_true",
                        help="try again where Bugbot refused before (once the Cursor side is fixed)")
    parser.add_argument("--grace-minutes", type=float, default=2,
                        help="leave heads pushed this recently to Bugbot's own review (default: %(default)s)")
    parser.add_argument("--cooldown-hours", type=float, default=24,
                        help="pause this long after Bugbot refuses our trigger (default: %(default)s)")
    parser.add_argument("--max-comments", type=int, default=10, help="per run (default: %(default)s)")
    args = parser.parse_args(argv)

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or gh_cli_token()
    if not token:
        print("bugbot_trigger: no GitHub token: set GH_TOKEN, or log in with `gh auth login`", file=sys.stderr)
        return 2
    authors = None if args.all_authors else {a.strip().lower() for a in args.only_authors.split(",") if a.strip()}
    try:
        run(GitHub(token), args.repo, authors, dry_run=args.dry_run, retry=args.retry,
            grace=timedelta(minutes=args.grace_minutes), cooldown=timedelta(hours=args.cooldown_hours),
            max_comments=args.max_comments, annotate=os.environ.get("GITHUB_ACTIONS") == "true")
    except GitHubError as exc:
        print("bugbot_trigger: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
