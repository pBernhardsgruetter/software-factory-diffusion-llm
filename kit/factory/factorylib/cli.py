"""``decide`` command line (``python -m factorylib.cli``; wrapped by ``factory/bin/decide``).

    decide <name> --job <id> [--state-file F] [--state-json S] [--image P]...
    decide toolgate [--state-file F] [--state-json S]
    decide outcome --job <id> --decision <name> --truth '<json>' [--note TEXT]
    decide report                      (Task 13; not implemented yet)

Prints exactly one JSON object on stdout. Exit 0 whenever a record is printed,
even if the Decide call itself failed (the error is in the record); exit 2 on a
usage error, an unknown decision, a bad job id, bad JSON or a bad image.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from .paths import find_root, load_job
from .routes import ROUTES
from .run import append_jsonl, now_iso, run_decision
from .state import build_state

_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class UsageError(Exception):
    """Exit 2 with this message on stderr."""


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(f"{self.prog}: {message}")


def _known(name: str) -> str:
    if name not in ROUTES:
        raise UsageError(f"unknown decision {name!r}; known: {', '.join(ROUTES)}")
    return name


def _root() -> Path:
    try:
        return find_root()
    except (subprocess.CalledProcessError, OSError) as exc:
        raise UsageError(f"not inside a git checkout: {exc}") from exc


def _job(root: Path, job_id: str):
    if not _JOB_ID.match(job_id):
        raise UsageError(f"bad job id {job_id!r}")
    try:
        return load_job(root, job_id)
    except FileNotFoundError as exc:
        raise UsageError(f"unknown job {job_id!r}: {exc} not found") from exc
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise UsageError(f"bad job {job_id!r}: unreadable job.json ({exc})") from exc


def _json_object(text: str, what: str) -> dict:
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise UsageError(f"{what}: invalid JSON ({exc})") from exc
    if not isinstance(obj, dict):
        raise UsageError(f"{what}: must be a JSON object")
    return obj


def _decide(argv: list[str]) -> dict:
    p = _Parser(prog="decide", description="Run one Mercury Decide decision.")
    p.add_argument("name")
    p.add_argument("--job")
    p.add_argument("--state-file")
    p.add_argument("--state-json")
    p.add_argument("--image", action="append", default=[])
    a = p.parse_args(argv)
    name = _known(a.name)
    if name == "toolgate":
        if a.job is not None:
            raise UsageError("toolgate takes no --job (it logs to factory/tool-gate.jsonl)")
    elif a.job is None:
        raise UsageError(f"decide {name}: --job is required")

    extra: dict = {}
    if a.state_file:
        try:
            text = Path(a.state_file).read_text()
        except OSError as exc:
            raise UsageError(f"--state-file {a.state_file}: {exc}") from exc
        extra.update(_json_object(text, f"--state-file {a.state_file}"))
    if a.state_json:
        extra.update(_json_object(a.state_json, "--state-json"))

    root = _root()
    job = _job(root, a.job) if a.job is not None else None
    try:
        state = build_state(name, root, job, extra)
        return run_decision(name, root=root, job=job, state=state,
                            images=[Path(i) for i in a.image])
    except (ValueError, OSError) as exc:  # bad image, template or config
        raise UsageError(f"decide {name}: {exc}") from exc


def _outcome(argv: list[str]) -> dict:
    p = _Parser(prog="decide outcome", description="Log the ground truth for a decision.")
    p.add_argument("--job", required=True)
    p.add_argument("--decision", required=True)
    p.add_argument("--truth", required=True)
    p.add_argument("--note")
    a = p.parse_args(argv)
    name = _known(a.decision)
    try:
        truth = json.loads(a.truth)
    except ValueError as exc:
        raise UsageError(f"--truth: invalid JSON ({exc})") from exc
    job = _job(_root(), a.job)
    record = {"kind": "outcome", "decision": name, "job": job.id, "truth": truth,
              "note": a.note, "ts": now_iso()}
    append_jsonl(job.folder / "decisions.jsonl", record)
    return record


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    try:
        if argv[:1] == ["report"]:
            raise UsageError("decide report: not implemented")
        record = _outcome(argv[1:]) if argv[:1] == ["outcome"] else _decide(argv)
    except UsageError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(json.dumps(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
