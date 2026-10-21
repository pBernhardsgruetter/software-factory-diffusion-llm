"""LLM judge for finished jobs: independent of the factory's own reviewers.

    judge job  --root DIR --job ID [--model M] [--out FILE]
    judge pair --a DIR:ID --b DIR:ID [--model M]

``job`` scores one merged job against the original request and the spec's
acceptance criteria, with evidence. ``pair`` compares two builds of the same
request, once in each order, and names a winner only when both orders agree.
The judge sees the request, the spec, the merged diff and the repo (read-only).
It never sees reviewer verdicts, the approver's decision or any Decide record,
so it cannot just agree with the factory.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

DIFF_CHARS = 60_000
TOOLS = "Read,Grep,Glob"

JOB_PROMPT = """You are an independent code judge. Judge ONE finished change.

Original request:
{request}

Specification the builder worked from (acceptance criteria):
{criteria}

The merged diff:
```diff
{diff}
```

The repository is checked out read-only at the merged state; use Read, Grep and Glob if the diff is not enough.
You do not see any reviewer or approver verdict. Judge the code itself.

Answer with ONE JSON object and nothing else:
{{"criteria": [{{"criterion": "<text>", "met": "yes|partial|no", "evidence": "<file:line and what it shows>"}}],
 "scores": {{"scope_discipline": 1-5, "simplicity": 1-5, "test_quality": 1-5, "copy_and_ux": 1-5}},
 "defects": [{{"severity": "high|medium|low", "where": "<file:line>", "what": "<defect>"}}],
 "summary": "<two sentences>"}}
Scores: 5 is excellent. scope_discipline 5 means nothing beyond the request. Only list defects you can point to."""

PAIR_PROMPT = """You are an independent code judge. Two builders implemented the SAME request. Decide which result is better.

Request:
{request}

Acceptance criteria:
{criteria}

=== Result X ===
```diff
{x}
```

=== Result Y ===
```diff
{y}
```

Weigh in this order: meets every criterion, correctness, scope discipline, simplicity, tests. Ignore length and style preferences.
Answer with ONE JSON object and nothing else:
{{"winner": "X|Y|tie", "reason": "<three sentences naming concrete differences>"}}"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60).stdout


def gather(root: Path, job: str) -> dict:
    """Request, criteria and merged diff of one job; nothing from the factory's verdicts."""
    jd = root / "factory" / "jobs" / job
    request = json.loads((jd / "job.json").read_text()).get("feature", "")
    spec = (jd / "spec.md").read_text()
    criteria = spec.split("## Acceptance criteria", 1)[1].split("\n## ", 1)[0].strip() if "## Acceptance criteria" in spec else spec
    sha = _git(root, "log", "--merges", "-1", "--format=%H", f"--grep=Merge factory/{job}").strip()
    diff = _git(root, "diff", f"{sha}^1", sha) if sha else ""
    if len(diff) > DIFF_CHARS:
        diff = diff[:DIFF_CHARS] + "\n[diff truncated]"
    return {"request": request, "criteria": criteria, "diff": diff, "root": root, "merge": sha}


def parse_json(text: str) -> dict | None:
    """The first balanced JSON object in the model's answer, or None."""
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            depth += (text[i] == "{") - (text[i] == "}")
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except ValueError:
                    break
        start = text.find("{", start + 1)
    return None


def ask(prompt: str, cwd: Path, model: str, runner=None) -> tuple[dict | None, float]:
    """One headless claude call, read-only tools, user settings off. Returns (answer, cost)."""
    cmd = ["claude", "-p", prompt, "--model", model, "--allowedTools", TOOLS, "--setting-sources", "project",
           "--strict-mcp-config", "--output-format", "json"]
    run = runner or (lambda c, d: subprocess.run(c, cwd=d, capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL))
    out = run(cmd, cwd)
    try:
        env = json.loads(out.stdout)
    except ValueError:
        return None, 0.0
    return parse_json(env.get("result", "")), float(env.get("total_cost_usd") or 0.0)


def judge_job(root: Path, job: str, model: str, runner=None) -> dict:
    g = gather(root, job)
    ans, cost = ask(JOB_PROMPT.format(**{k: g[k] for k in ("request", "criteria", "diff")}), root, model, runner)
    res = {"job": job, "model": model, "cost_usd": round(cost, 4), "merge": g["merge"], "answer": ans}
    if ans:
        c = ans.get("criteria") or []
        res["criteria_met"] = sum(1 for x in c if x.get("met") == "yes")
        res["criteria_total"] = len(c)
        res["defects_high"] = sum(1 for d in ans.get("defects") or [] if d.get("severity") == "high")
    return res


def judge_pair(a: tuple[Path, str], b: tuple[Path, str], model: str, runner=None, rng=None) -> dict:
    """Both orders; a winner only when the two verdicts agree on the same build."""
    ga, gb = gather(*a), gather(*b)
    verdicts, cost = [], 0.0
    for first, second, label in ((ga, gb, "ab"), (gb, ga, "ba")):
        p = PAIR_PROMPT.format(request=ga["request"], criteria=ga["criteria"], x=first["diff"], y=second["diff"])
        ans, c = ask(p, a[0], model, runner)
        cost += c
        w = (ans or {}).get("winner")
        mapped = {"X": label[0], "Y": label[1]}.get(w, "tie")  # which build won, by position
        verdicts.append({"order": label, "winner_build": mapped, "reason": (ans or {}).get("reason")})
    w1, w2 = verdicts[0]["winner_build"], verdicts[1]["winner_build"]
    return {"a": f"{a[0].name}:{a[1]}", "b": f"{b[0].name}:{b[1]}", "model": model, "cost_usd": round(cost, 4),
            "winner": w1 if w1 == w2 and w1 in ("a", "b") else "tie", "verdicts": verdicts,
            "position_biased": w1 != w2 and "tie" not in (w1, w2)}


def _side(s: str) -> tuple[Path, str]:
    d, _, j = s.rpartition(":")
    return Path(d).resolve(), j


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="judge", description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("job")
    j.add_argument("--root", type=Path, default=Path.cwd())
    j.add_argument("--job", required=True)
    j.add_argument("--out", type=Path)
    q = sub.add_parser("pair")
    q.add_argument("--a", required=True, help="DIR:JOB")
    q.add_argument("--b", required=True, help="DIR:JOB")
    for s in (j, q):
        s.add_argument("--model", default="claude-opus-5-5")
    a = p.parse_args(argv)
    if a.cmd == "job":
        res = judge_job(a.root.resolve(), a.job, a.model)
        if a.out:
            a.out.write_text(json.dumps(res, indent=1))
    else:
        res = judge_pair(_side(a.a), _side(a.b), a.model)
    sys.stdout.write(json.dumps(res, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
