import json
import shutil
from pathlib import Path

import pytest

from factorylib.policy import Policy, load_policy

REAL = Path(__file__).resolve().parent.parent / "policy.json"


@pytest.fixture
def policy(tmp_path):
    (tmp_path / "factory").mkdir()
    shutil.copy(REAL, tmp_path / "factory" / "policy.json")
    return load_policy(tmp_path)


def test_load_policy_reads_factory_policy_json(tmp_path):
    (tmp_path / "factory").mkdir()
    (tmp_path / "factory" / "policy.json").write_text(json.dumps({
        "escalate_paths": ["x/**"], "tool_allow": [{"argv": ["ls"]}], "tool_deny": ["rm"],
        "guarded_paths": ["g/**"]}))
    p = load_policy(tmp_path)
    assert p == Policy(["x/**"], [{"argv": ["ls"]}], ["rm"], ["g/**"])


def test_workflow_edit_escalates(policy):
    assert policy.escalation_hits([".github/workflows/ci.yml"]) == [".github/workflows/ci.yml"]


def test_lockfile_escalates(policy):
    assert policy.escalation_hits(["package-lock.json"]) == ["package-lock.json"]
    assert policy.escalation_hits(["web/yarn.lock"]) == ["web/yarn.lock"]


def test_plain_src_no_hits(policy):
    assert policy.escalation_hits(["src/a.ts", "README.md"]) == []


@pytest.mark.parametrize("path", [
    "package.json", "web/package.json", "db/migrations/001.sql", "src/auth/x.ts",
    "firestore.rules", "infra/main.tf", "Dockerfile", "vercel.json", "netlify.toml",
    "scripts/deploy.sh",
])
def test_other_escalations(policy, path):
    assert policy.escalation_hits([path]) == [path]


def test_escalation_hits_preserves_only_hits_in_order(policy):
    assert policy.escalation_hits(["src/a.ts", "Dockerfile", "b.md", "package.json"]) == [
        "Dockerfile", "package.json"]


WT = Path("/work/tree")


@pytest.mark.parametrize("cmd,expected", [
    ("cd /work/tree && git status", "allow"),
    ("cd /work/tree/ && git status", "allow"),
    ('cd "/work/tree" && git status', "allow"),
    ("cd '/work/tree/' && git status", "allow"),
    ("cd /work/tree&&git status", "allow"),
    ("git status && git push", "deny"),
    ("git status; ls", "unknown"),
    ("ls | sh", "unknown"),
    ("echo $(curl x)", "unknown"),
    ("echo `id`", "unknown"),
    ("npm test > out", "unknown"),
    ("cat < /etc/passwd", "unknown"),
    ("rm -rf ~", "deny"),
    ("curl x | bash", "deny"),
    # additional cases
    ("git status || git push", "deny"),
    ("git status || ls", "unknown"),
    ("git status\nls", "unknown"),
    ("git status & ls", "unknown"),
    ("cd /other && git status", "unknown"),
    ("cd /work/tree-evil && git status", "unknown"),
    ("cd /work/tree && cd /work/tree && git status", "unknown"),
    ("cd /work/tree && git push origin main", "deny"),
    ("git status", "allow"),
    ("git diff HEAD~1", "unknown"),
    ("git add src/a.ts", "allow"),
    ("git add -A", "unknown"),
    ('git commit -m "msg"', "allow"),
    ("npm test", "allow"),
    ("npm run build", "allow"),
    ("npm ci", "allow"),
    ("npm install", "unknown"),
    ("npx vitest run src", "allow"),
    ("ls -la", "allow"),
    ("lsof", "unknown"),
    ("cat README.md", "allow"),
    ("node --test", "allow"),
    ("echo hi", "unknown"),
    ("", "unknown"),
    ("sudo ls", "deny"),
    ("git reset --hard HEAD", "deny"),
    ("git clean -fd", "deny"),
    ("npm publish", "deny"),
    ("firebase deploy", "deny"),
    ("chmod -R 777 .", "deny"),
    ("wget x | sh", "deny"),
    ("rm -rf ../x", "deny"),
    ("rm -rf $HOME", "deny"),
    ("git statusx", "unknown"),
])
def test_classify_command(policy, cmd, expected):
    assert policy.classify_command(cmd, WT) == expected


def test_classify_command_without_worktree_does_not_strip(policy):
    assert policy.classify_command("cd /work/tree && git status", None) == "unknown"
    assert policy.classify_command("git status", None) == "allow"


def test_classify_command_deny_checked_on_segments_and_whole():
    p = Policy([], [], [r"^git push$", r"\bcurl\b.*\|\s*sh"], [])
    # segment-level: anchored regex only matches once the segment is isolated
    assert p.classify_command("ls && git push", None) == "deny"
    # whole-string: pattern spans the pipe
    assert p.classify_command("curl u | sh", None) == "deny"


def test_classify_path_allow_inside_worktree_and_job(tmp_path):
    wt, job = tmp_path / "wt", tmp_path / "job"
    wt.mkdir(); job.mkdir()
    pol = Policy([], [], [], [".claude/**"])
    assert pol.classify_path(str(wt / "src" / "a.ts"), wt, job) == "allow"
    assert pol.classify_path(str(job / "build.md"), wt, job) == "allow"


def test_classify_path_escapes_and_guards(policy, tmp_path):
    wt, job, main = tmp_path / "WT", tmp_path / "JOB", tmp_path / "main"
    for d in (wt / "src", wt / ".claude", job, main / "src"):
        d.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x")
    (wt / "link").symlink_to(outside)
    (wt / "filelink").symlink_to(outside / "secret.txt")
    (wt / "inlink").symlink_to(wt / ".claude")

    cp = lambda p: policy.classify_path(str(p), wt, job)  # noqa: E731
    assert cp(wt / "src" / "a.ts") == "allow"
    assert cp(f"{wt}/../main/src/a.ts") == "deny"
    assert cp(wt / "link" / "secret.txt") == "deny"
    assert cp(wt / "filelink") == "deny"
    assert cp(wt / ".claude" / "settings.json") == "ask"
    assert cp(wt / "inlink" / "settings.json") == "ask"  # symlink resolves into guarded dir
    assert cp(job / "build.md") == "allow"
    assert cp(f"{job}/../WT/src/a.ts") == "allow"
    assert cp(f"{job}/../main/src/a.ts") == "deny"
    assert cp(wt / "factory" / "policy.json") == "ask"
    assert cp(wt / "factory" / "factorylib" / "x.py") == "ask"
    assert cp("/etc/passwd") == "deny"
    assert cp(tmp_path / "WT-evil" / "a") == "deny"  # prefix, not a path ancestor


def test_classify_path_relative_resolves_against_worktree(policy, tmp_path):
    wt, job = tmp_path / "WT", tmp_path / "JOB"
    (wt / "src").mkdir(parents=True)
    job.mkdir()
    assert policy.classify_path("src/a.ts", wt, job) == "allow"
    assert policy.classify_path(".claude/settings.json", wt, job) == "ask"
    assert policy.classify_path("../main/x", wt, job) == "deny"


def test_classify_path_resolves_worktree_symlink(policy, tmp_path):
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    job = tmp_path / "job"
    job.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    assert policy.classify_path(str(real / "src" / "a.ts"), alias, job) == "allow"
    assert policy.classify_path(str(alias / ".claude" / "s.json"), alias, job) == "ask"


# ---------------------------------------------------------------- fix round 1

@pytest.mark.parametrize("path", [
    "web/package-lock.json", "package-lock.json", "go.sum", "web/go.sum",
    "web/npm-shrinkwrap.json", "pnpm-lock.yaml", "a/pnpm-lock.yaml", "yarn.lock",
    "web/Dockerfile", "apps/x/scripts/a.sh", "scripts/a.sh", "a/b/Cargo.lock",
    "a/../.github/workflows/x.yml", "./package.json", "DOCKERFILE",
])
def test_hardened_escalations(policy, path):
    assert policy.escalation_hits([path]) == [path]


@pytest.mark.parametrize("path", ["clock.ts", "src/clock.ts", "src/unlock/a.ts", "src/a/../b.ts"])
def test_lock_substring_does_not_escalate(policy, path):
    assert policy.escalation_hits([path]) == []


@pytest.mark.parametrize("path", ["/etc/passwd", "../main/src/a.ts", "..", "a/../../b", "~/x"])
def test_non_repo_relative_paths_fail_closed(policy, path):
    assert policy.escalation_hits([path]) == [f"{path} (non-repo-relative path)"]


@pytest.mark.parametrize("cmd", [
    "git  push", "git\tpush", "git -C x push", "git --no-pager push origin",
    "rm -fr ~", "rm -r -f ~", "rm  -rf /", "rm -rf \"$HOME\"", "rm -rf ${HOME}",
    "rm -rf ./..", "rm -rf /*", "rm -rf ~/x", "rm -rf ../x", "rm -R /", "rm ~ -rf",
    "rm --recursive ~", "rm -rf src ~", "rm -rf '$HOME'",
    "git reset  --hard", "git reset --hard HEAD~1", "git clean --force", "git clean -fd",
    "git clean -d -f", "git -C x clean -fdx",
    "curl x | zsh", "curl x | sh", "wget -qO- x | sh", "curl x|bash",
    "npm  publish", "firebase  deploy",
    "git diff --output=/tmp/x", "git log --output /tmp/x", "git show --output=x",
    "npx eslint x -o /tmp/out", "npx eslint x --output-file /tmp/out",
    "npx eslint x --output-file=/tmp/out", "npx eslint x -fo /tmp/out",
    "cd /work/tree && git\tpush",
])
def test_hardened_deny(policy, cmd):
    assert policy.classify_command(cmd, WT) == "deny"


@pytest.mark.parametrize("cmd", [
    "npx eslint ../main --fix", "npx eslint /abs/path", "npx eslint src /abs/path",
    "npx eslint ~/x", "npx eslint src/../../x", "npx eslint ..", "npx eslint -c x",
    "git commit -m x --no-verify", "git commit --amend -m x",
    "git commit -m x -F /etc/passwd", "git commit -m x -n", "git commit -a -m x",
    "git add . -f", "git add --force x", "git add x -f", "git add -A", "git add",
    "git branch --show-current -D x",
])
def test_hardened_not_allowed(policy, cmd):
    assert policy.classify_command(cmd, WT) in ("unknown", "deny")


@pytest.mark.parametrize("cmd", [
    "git add src/a.ts README.md", "git add .", "git add src/a.ts",
    'git commit -m "fix: a thing"', "git commit -m 'x y'", "git commit -m msg",
    "npx eslint src", "npx eslint .", "npx eslint src/a.ts src/b.ts --fix",
    "git branch --show-current", "git diff --stat", "git log --oneline -n 5",
    "npx   eslint   src", "git  status",
])
def test_normal_commands_stay_allowed(policy, cmd):
    assert policy.classify_command(cmd, WT) == "allow"


def test_classify_path_git_and_claude_guarded_case_insensitive(policy, tmp_path):
    wt, job = tmp_path / "WT", tmp_path / "JOB"
    for d in (wt / ".git", wt / ".claude", job):
        d.mkdir(parents=True)
    cp = lambda p: policy.classify_path(str(p), wt, job)  # noqa: E731
    assert cp(wt / ".git" / "config") == "ask"
    assert cp(wt / ".git") == "ask"
    assert cp(wt / ".claude") == "ask"
    assert cp(wt / ".CLAUDE" / "settings.json") == "ask"
    assert cp(wt / ".Git" / "config") == "ask"
    assert cp(wt / "Factory" / "Policy.json") == "ask"
    assert cp(wt / "src" / "a.ts") == "allow"
    assert cp(wt / ".github" / "x.yml") == "allow"  # guarded set is exact, not a prefix


@pytest.mark.parametrize("path", ["~", "~/x", "~root/.ssh/id_rsa", "~/../x"])
def test_classify_path_tilde_denied_before_resolve(policy, tmp_path, path, monkeypatch):
    wt, job = tmp_path / "WT", tmp_path / "JOB"
    wt.mkdir(); job.mkdir()
    # a literal "~" dir inside the worktree must not rescue it
    (wt / "~").mkdir()
    monkeypatch.chdir(wt)
    assert policy.classify_path(path, wt, job) == "deny"


# ---------------------------------------------------------------- fix round 2
import time  # noqa: E402

BYPASSES = [
    # shell quoting / escaping / brace / glob expansion happens before git runs
    'git add "-f" src/a.ts', "git add src/a.ts '--force'", r"git add \-f src/a.ts",
    "git add {-f,src/a.ts}",
    "git commit -m {x,--no-verify}", "git commit -m {x,--amend}", "git commit -m *",
    "git add *", "git add src/*", "git add ?", "git add [a]", "git commit -m ?",
    'git diff "--output=/tmp/x"', r"git diff \--output=/tmp/x",
    "git diff {--output=/tmp/x,HEAD}",
    'npx eslint src "../main" --fix', 'npx eslint src "/abs/path" --fix',
    "npx eslint src '/etc' --fix", "npx eslint src $HOME/x --fix",
    "npx eslint {--fix,../main}", "npx eslint {--output-file,/tmp/out} src",
    r"npx eslint src \-o out.txt", "npx eslint src --rulesdir=../evil",
    "npx eslint src -c=../evil.mjs", "npx eslint src --config ../evil",
    "npx eslint ~/x", "npx eslint src ~",
    # message exception must stay narrow
    'git commit -m "a $(id)"', 'git commit -m "a `id`"', r'git commit -m "a\"b"',
    'git commit -m "$HOME"', 'git commit -m "x" --no-verify', 'git commit -m "a";ls',
    "git commit -m '--amend' -n", "git commit -m --amend", 'git commit -m "a" "b"',
    # env prefixes, options, unlisted subcommands
    "FOO=1 git status", "git -c core.pager=x status", "git -C x status",
    "git --git-dir=/x status", "git status --ignored", "git diff --ext-diff",
    "git diff --textconv", "git diff -o x", "git log -c", "git log -C x",
    "git log --exec-path", "git show --work-tree=/x", "git diff /etc/passwd",
    "git diff ../x", "git diff a/../../b", "git log -n x",
    "git log -n", "git log -5", "git diff HEAD~1",
    "git add", "git add -A", "git add ../x", "git add /etc/x", "git add a/../../b",
    "git branch", "git branch --show-current x", "git branch -D x",
    "npm test -- --x", "npm ci x", "npm run build x", "npm run other",
    "npm install", "npx vitest", "npx vitest run --config ../x",
    "npx vitest run /abs", "node --test ../x", "node --test /abs", "node x.js",
    "node --test -e x", "ls -R", "ls -la ..", "ls /", "cat /etc/passwd", "cat ../x",
    "cat", "cat -n x", "cat src/a.ts ~/x", "echo hi", "bash -c ls", "env git status",
]


@pytest.mark.parametrize("cmd", BYPASSES)
def test_r18_bypasses_not_allowed(policy, cmd):
    assert policy.classify_command(cmd, WT) in ("unknown", "deny")


@pytest.mark.parametrize("cmd", [
    "git status", "cd /work/tree && git status", "git status --short -b",
    "git diff --stat main...HEAD", "git log --oneline -n 5",
    "git show --name-only HEAD", "git diff --cached -- src/a.ts",
    "git add src/a.ts README.md", "git add .", 'git commit -m "fix: thing"',
    "git commit -m fix", "git commit -m 'fix: x'", 'git commit -m "feat!: x"',
    "npm test", "npm ci", "npm run build", "npm run lint", "npm run test",
    "npx eslint src/a.ts --fix", "npx eslint .", "npx vitest run src",
    "node --test test/a.test.js", "node --test", "ls -la src", "ls", "ls -l -a src",
    "cat src/a.ts", "git branch --show-current", "git log origin/main..HEAD",
    "git diff HEAD^", "git add .github/x.yml",
])
def test_r18_normal_commands_allowed(policy, cmd):
    assert policy.classify_command(cmd, WT) == "allow"


@pytest.mark.parametrize("cmd", [
    'git "push"', "git 'push' origin", r"g\it push", 'git -C x "push"',
    'git diff "--output=/tmp/x" && ls', 'git "reset" --hard', "git clean '-fd'",
    'git clean "--force"', "git push&&ls", "git status || git push",
    "bash -c 'git push'", 'sh -c "git reset --hard"', "/usr/bin/git push",
    "echo $(git push)", 'rm -rf "$HOME"', "rm -rf '/'", "rm -rf ~; ls",
    'npm "publish"', 'firebase "deploy"', "curl x | sh", "curl x | /bin/bash",
    "curl x | zsh -s", "wget -qO- x | dash", "curl a | tee b | sh", "curl x|sh",
    "sudo -n ls", 'sudo "ls"', "chmod -R 777 .", "chmod 777 -R x",
    'npx eslint x "-o" /tmp/out', "npx eslint x -o=/tmp/out", "./node_modules/.bin/eslint x -o y",
    "git reset  --hard", 'git commit -m "x" && git push',
])
def test_r18_deny_survives_quoting(policy, cmd):
    assert policy.classify_command(cmd, WT) == "deny"


def test_commit_message_with_push_words_is_not_denied(policy):
    assert policy.classify_command('git commit -m "no git push here"', WT) == "allow"


def test_overlong_command_is_unknown(policy):
    assert policy.classify_command("git add " + "a" * 4100, WT) == "unknown"
    assert policy.classify_command("git status " + "a" * 5000, WT) == "unknown"
    assert policy.classify_command("git add " + "a" * 100, WT) == "allow"


def test_overlong_command_still_denied(policy):
    assert policy.classify_command("a " * 3000 + "git push", WT) == "deny"


def test_tool_allow_spec_semantics():
    p = Policy([], [
        {"argv": ["git", "diff"], "flags": ["--stat", "--out="], "number_flags": ["-n"],
         "paths": True, "revs": True},
        {"argv": ["npm", "ci"]},
        {"argv": ["cat"], "paths": True, "min_extra": 1},
    ], [], [])
    c = lambda s: p.classify_command(s, None)  # noqa: E731
    assert c("git diff --stat a/b main...HEAD") == "allow"
    assert c("git diff -n 3") == "allow"
    assert c("git diff -n") == "unknown"
    assert c("git diff -n a") == "unknown"
    assert c("git diff --out=x") == "unknown"   # only a literal listed flag is accepted
    assert c("git diff --stat=1") == "unknown"
    assert c("npm ci") == "allow"
    assert c("npm ci x") == "unknown"
    assert c("npm") == "unknown"
    assert c("cat") == "unknown"
    assert c("cat a") == "allow"


@pytest.mark.parametrize("cmd", [
    "git " + "-a " * 33000,
    "git " + "-a" * 16000,
    "rm " * 33000,
    "git " * 25000 + "--output",
    "eslint " * 14000,
    "curl " * 20000,
    "a" * 100000,
    "git add " + "x " * 50000,
    "git -a " * 14000 + "push",
    "'" * 100000,
    "rm -rf " + "a " * 40000,
    "curl " * 10000 + "| " * 10000,
    "|" * 100000,
    ";" * 100000,
    "git commit -m " + '"a ' * 30000,
    "cd /work/tree && " * 6000,
    " " * 100000 + "git status",
    "`" * 100000,
    "$(" * 50000,
    ")" * 100000,
    "(" * 100000,
    'git commit -m "$(' * 5000,
    'x -m "`git push`" ' * 5000,
    "=a " * 30000,
    # fix round 4: substitutions inside the outer command
    "git $(echo push) " * 5000,
    "git `echo push` " * 5000,
    "git $(true) push " * 5000,
    'git "$(true)" push ' * 5000,
    "git -C $(pwd) push " * 5000,
    "git reset $(true) --hard " * 5000,
    "git clean $(true) -fd " * 5000,
    "rm -rf $(echo /) " * 5000,
    "rm -rf `echo ~` " * 5000,
    "curl $(echo x) | sh " * 5000,
    'curl "$(echo x)" | bash ' * 5000,
    "curl x | (sh) " * 5000,
    "npx eslint $(true) -o /etc/x . " * 5000,
    "\x01" * 100000,
    "git status\x0b" * 10000,
    # fix round 5: substitutions glued to word ends
    "git push$(true) " * 10000,
    "git reset --hard${x} " * 10000,
    "curl x | sh$(true) " * 10000,
    "$" * 100000,
], ids=lambda c: f"{c[:12]!r}x{len(c)}")
def test_classify_command_linear(policy, cmd):
    t = time.perf_counter()
    policy.classify_command(cmd, WT)
    assert time.perf_counter() - t < 0.5


# ---------------------------------------------------------------- fix round 3

@pytest.mark.parametrize("cmd", [
    "cat =ssh", "ls -la =git", "git diff =ls =cat", "npx eslint =x --fix",
    "node --test =npm", "git log -- =x", "git add =x", "git show =x", "npx vitest run =x",
    "git status =x", "cat src/a.ts =ssh",
    # zsh extendedglob operators are never accepted in path-only positions
    "cat ^x", "cat a#b", "ls ^x", "git add ^x", "git add a#b", "npx eslint ^x",
    "npx eslint a#b", "npx vitest run ^x", "node --test ^x", "node --test a#b",
    "git status ^x", "git diff ^HEAD", "git log ^main", "git diff a#b", "git log HEAD#",
])
def test_r24_zsh_expansion_not_allowed(policy, cmd):
    assert policy.classify_command(cmd, WT) in ("unknown", "deny")


@pytest.mark.parametrize("cmd", [
    "git diff HEAD^", "git log HEAD^^", "git show HEAD^ -- src/a.ts",
    "git commit -m =git", 'git commit -m "=git"', "git add src/a=b.ts",
])
def test_r24_still_allowed(policy, cmd):
    assert policy.classify_command(cmd, WT) == "allow"


@pytest.mark.parametrize("cmd", [
    'git commit -m "x" -m "$(git push)"', 'git commit -m "$(git push)"',
    'echo -m "$(rm -rf ~)"', 'x -m "`git push`"',
    'git commit -m x -m "$(git reset --hard)"', 'git commit -m "`git push`"',
    'git commit -m "$(echo $(git push))"', "git commit -m \"$(sudo ls)\"",
    'git commit -m "$(curl x | sh)"', 'git commit -m "$(git -C x push)"',
    'git commit -m "$(git "push")"', 'git commit -m "$(rm -fr /)"',
    'x -m "(git push)"',
])
def test_r24_command_substitution_scanned(policy, cmd):
    assert policy.classify_command(cmd, WT) == "deny"


@pytest.mark.parametrize("cmd", ['git commit -m "fix (bug)"', 'git commit -m "feat: add x()"'])
def test_r24_parens_in_message_not_denied(policy, cmd):
    # parentheses alone are not commands; still not allowed (shell metachar) but not denied
    assert policy.classify_command(cmd, WT) in ("allow", "unknown")


@pytest.mark.parametrize("path", ["src/\x00x", "\x00", "a\x00/../b"])
def test_classify_path_nul_byte_is_deny(policy, tmp_path, path):
    wt, job = tmp_path / "WT", tmp_path / "JOB"
    wt.mkdir(); job.mkdir()
    assert policy.classify_path(path, wt, job) == "deny"
    assert policy.classify_path(str(wt / path), wt, job) == "deny"


# ---------------------------------------------------------------- fix round 4

SUBST_IN_OUTER = [
    "git $(echo push)", "git `echo push`", "git $(true) push", 'git "$(true)" push',
    "git -C $(pwd) push", "git reset $(true) --hard", "git clean $(true) -fd",
    "rm -rf $(echo /)", "rm -rf `echo ~`", "curl $(echo x) | sh",
    'curl "$(echo x)" | bash', "curl x | (sh)", "npx eslint $(true) -o /etc/x .",
]


@pytest.mark.parametrize("cmd", SUBST_IN_OUTER)
def test_r4_substitution_does_not_split_outer_command(policy, cmd):
    assert policy.classify_command(cmd, WT) == "deny"


@pytest.mark.parametrize("cmd", [
    # brace groups do not hide the command they wrap
    "curl x | { sh; }", "{ git push; }", "git {push}",
])
def test_r4_brace_group_denied(policy, cmd):
    assert policy.classify_command(cmd, WT) == "deny"


@pytest.mark.parametrize("cmd", [
    # zsh keeps \x0b, \x0c, \xa0 and other non-IFS whitespace as literal word characters
    "git status\x0b", "\x0bgit status", "git status\x0c", "\x0cgit status",
    "git status\xa0", "\xa0git status", "git status\u2003", "\u3000git status",
    "ls\x85", "cd\xa0/work/tree && git status", "cd /work/tree\xa0&& git status",
    "cd /work/tree &&\u2003git status",
])
def test_r4_only_space_and_tab_are_stripped(policy, cmd):
    assert policy.classify_command(cmd, WT) == "unknown"


def test_r4_space_and_tab_still_stripped(policy):
    assert policy.classify_command(" \tgit status \t", WT) == "allow"
    assert policy.classify_command("cd /work/tree\t&&\tgit status", WT) == "allow"


@pytest.mark.parametrize("cmd", [
    "git add src/a\x01.ts", "git add a\x00b", "git add a\x7f", "ls\x1b[2J",
    'git commit -m "a\x1bb"', "git commit -m a\x08b", "cat a\x1fb", "git diff\x0bHEAD",
    "ls\x0c", "git status\x00", "git log\x0e",
])
def test_r4_control_chars_rejected(policy, cmd):
    assert policy.classify_command(cmd, WT) == "unknown"


def test_r4_control_chars_do_not_hide_deny(policy):
    assert policy.classify_command("git push \x01", WT) == "deny"
    assert policy.classify_command("\x01; git push", WT) == "deny"


# ---------------------------------------------------------------- fix round 5

@pytest.mark.parametrize("cmd", [
    "git push$(true)", "git push$()", "git push${x}",
    "git reset --hard$(true)", "git reset --hard${x}", "git clean -fd$(true)",
    "curl x | sh$(true)", "curl x | bash${x}",
    # extra: quoted / backtick forms of the same gluing
    'git push"$(true)"', "git push`true`", "git push$x",
])
def test_r5_substitution_glued_to_word_end_denied(policy, cmd):
    assert policy.classify_command(cmd, WT) == "deny"


@pytest.mark.parametrize("cmd,expected", [
    ("rm -rf $HOME", "deny"), ("rm -rf ${HOME}", "deny"),
    ('git commit -m "fix (bug)"', "allow"), ('git commit -m "(push)"', "allow"),
    ("git status", "allow"), ("git diff HEAD^", "allow"),
])
def test_r5_dollar_mapping_preserves(policy, cmd, expected):
    assert policy.classify_command(cmd, WT) == expected


# ------------------------------------------------------- final review (I2)

@pytest.mark.parametrize("cmd", [
    "rm -rf $(true) $HOME", "rm -rf ${x}$HOME", "rm -rf ($HOME)",
    "rm -rf `true` $HOME", "rm -rf {$HOME}", "rm -rf $(true) ${HOME}",
    "rm -fr ( $HOME )", "cd x && rm -rf $(true) $HOME",
])
def test_i2_third_scan_keeps_dollar(policy, cmd):
    assert policy.classify_command(cmd, WT) == "deny"


@pytest.mark.parametrize("cmd,expected", [
    ('git commit -m "fix (bug)"', "allow"), ("git status", "allow"),
    ("rm -rf $(true) build", "unknown"), ("rm -rf (build)", "unknown"),
])
def test_i2_third_scan_does_not_overreach(policy, cmd, expected):
    assert policy.classify_command(cmd, WT) == expected


@pytest.mark.parametrize("cmd", [
    "rm -rf $(true) $HOME " * 5000, "rm -rf ${x}$HOME " * 5000, "rm -rf ($HOME) " * 5000,
    "(" * 100000, "{" * 100000,
], ids=lambda c: f"{c[:12]!r}x{len(c)}")
def test_i2_third_scan_linear(policy, cmd):
    t = time.perf_counter()
    assert policy.classify_command(cmd, WT) in ("deny", "unknown")
    assert time.perf_counter() - t < 0.5


# ------------------------------------------------------- final review (M1, M2)

@pytest.mark.parametrize("path", [
    "bun.lockb", "web/bun.lockb", "Dockerfile.dev", "svc/Dockerfile.prod", "Containerfile",
    "svc/Containerfile", "docker-compose.yml", "docker-compose.override.yml",
    "ops/docker-compose.prod.yaml", "compose.yml", "deploy/compose.prod.yml", "compose.yaml",
    ".github/workflows/ci.yml", "pkg/.github/workflows/x.yml", "a/b/.github/CODEOWNERS",
])
def test_m1_more_escalations(policy, path):
    assert policy.escalation_hits([path]) == [path]


@pytest.mark.parametrize("path", ["src/compose.ts", "docs/dockerfiles.md", "src/bun.ts"])
def test_m1_no_overreach(policy, path):
    assert policy.escalation_hits([path]) == []


@pytest.mark.parametrize("rel", [".husky/pre-commit", ".husky/_/husky.sh", ".mcp.json",
                                 "CLAUDE.md", "AGENTS.md", "pkg/CLAUDE.md", "a/b/AGENTS.md",
                                 "claude.md", "Agents.md"])
def test_m2_more_guarded_paths(policy, tmp_path, rel):
    wt, job = tmp_path / "wt", tmp_path / "job"
    wt.mkdir(); job.mkdir()
    assert policy.classify_path(str(wt / rel), wt, job) == "ask"


@pytest.mark.parametrize("rel", ["docs/AGENTS.txt", "src/claude.ts", "mcp.json", "husky/x"])
def test_m2_guarded_no_overreach(policy, tmp_path, rel):
    wt, job = tmp_path / "wt", tmp_path / "job"
    wt.mkdir(); job.mkdir()
    assert policy.classify_path(str(wt / rel), wt, job) == "allow"


# ------------------------------------------------------- final review (M4)

@pytest.mark.parametrize("cmd,expected", [
    ("git rev-parse HEAD", "allow"), ("cd /work/tree && git rev-parse HEAD", "allow"),
    ("git rev-parse", "unknown"), ("git rev-parse HEAD~1", "unknown"),
    ("git rev-parse HEAD x", "unknown"), ("git rev-parse --show-toplevel", "unknown"),
    ("git rev-parse --git-dir HEAD", "unknown"), ("git rev-parse main", "unknown"),
])
def test_m4_rev_parse_head_exactly(policy, cmd, expected):
    assert policy.classify_command(cmd, WT) == expected
