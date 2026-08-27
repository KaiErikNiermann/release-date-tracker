"""Every project rule must still match the thing it was written for.

A semgrep pattern that stops matching is indistinguishable from a clean codebase: the scan
goes green, the rule is dead, and nobody finds out until the bug it guarded ships again.
That is not hypothetical here — on the first draft of `.semgrep/`, **nine of eleven rules
matched nothing**, for three separate reasons:

* a `pattern-not` ending in `...` cancels its own positive pattern, because `...` matches
  zero statements;
* semgrep's built-in ignore list skips `tests/`, so a rule aimed at test code never saw a
  file;
* `paths: include:` is relative to the scan root, so it silently excluded everything when
  the root moved.

None of those produce an error. So each rule gets a deliberately broken snippet here, and
this asserts the rule finds it. The fixture is written to a temp directory rather than
committed, so it never has to be excluded from ruff, pyright and the real semgrep run.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

RULES_DIR = Path(__file__).resolve().parent.parent / ".semgrep"

# One violation per rule id. The key is the rule the snippet must trip.
VIOLATIONS: dict[str, str] = {
    "naive-datetime-now": "def f():\n    return datetime.now()\n",
    "utcnow-is-deprecated-and-naive": "def f():\n    return datetime.utcnow()\n",
    "unwrap-credentials-through-the-helper": (
        "def f(s):\n    return s.tmdb_api_key.get_secret_value()\n"
    ),
    "secretstr-duck-typed-instead-of-checked": (
        'def f(x):\n    return hasattr(x, "get_secret_value")\n'
    ),
    "str-on-a-credential-renders-the-mask": ('def f(s):\n    return f"Bearer {s.notion_token}"\n'),
    "append-only-loop-should-be-a-comprehension": (
        "def f(items):\n    out = []\n    for i in items:\n        out.append(i)\n    return out\n"
    ),
    "use-clock-utc-today": "def f():\n    return datetime.now(UTC).date()\n",
    "use-clock-utc-now": "def f():\n    return datetime.now(UTC)\n",
    "use-field-name-for": (
        "def f(alias):\n"
        "    for name, field in Settings.model_fields.items():\n"
        "        if field.alias == alias:\n"
        "            return name\n"
        "    return None\n"
    ),
    "use-the-shared-until-helper": (
        "async def _until(pilot, predicate, what, timeout=5.0):\n"
        "    while True:\n"
        "        await pilot.pause()\n"
        '    raise AssertionError(f"timed out waiting for {what}")\n'
    ),
    "write-config-through-the-atomic-writer": (
        "def f(text):\n    path = config_file_path()\n    path.write_text(text)\n"
    ),
}


def _rule_ids() -> set[str]:
    return {
        rule["id"]
        for path in sorted(RULES_DIR.glob("*.yml"))
        for rule in yaml.safe_load(path.read_text())["rules"]
    }


def _scan(target: Path) -> dict[str, int]:
    """Run the project's rules over ``target``, returning hits per rule id.

    The venv's `semgrep` is a shim that execs `pysemgrep` off PATH, and on a machine with a
    system semgrep installed that copy wins and dies on an unrelated import. Putting the
    venv's own bin first is what makes this reproducible outside `poetry run`.
    """
    env = dict(os.environ)
    env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            str(Path(sys.executable).parent / "semgrep"),
            "--config",
            str(RULES_DIR),
            "--quiet",
            "--json",
            "--disable-version-check",
            str(target),
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if not result.stdout.strip():
        pytest.skip(f"semgrep unavailable: {result.stderr.strip()[:200]}")
    payload = json.loads(result.stdout)
    hits: dict[str, int] = {}
    for finding in payload["results"]:
        rule = str(finding["check_id"]).split(".")[-1]
        hits[rule] = hits.get(rule, 0) + 1
    return hits


@pytest.fixture(scope="module")
def fixture_hits(tmp_path_factory: pytest.TempPathFactory) -> dict[str, int]:
    """Write one violation per rule and scan them in a single semgrep run."""
    root = tmp_path_factory.mktemp("semgrep_fixture")
    # Under `src/`, because the atomic-writer rule is scoped there — and NOT under a path
    # named `tests/`, which semgrep's built-in ignore list would skip.
    package = root / "src" / "release_tracker"
    package.mkdir(parents=True)
    header = (
        "from datetime import UTC, datetime\n"
        "from release_tracker.config import Settings, config_file_path\n\n"
    )
    for rule, snippet in VIOLATIONS.items():
        (package / f"{rule.replace('-', '_')}.py").write_text(header + snippet)
    return _scan(root)


def test_every_rule_has_a_violation_to_check() -> None:
    """A rule with no fixture is a rule nobody has proven works."""
    assert _rule_ids() == set(VIOLATIONS), "add a violation snippet for each new rule"


@pytest.mark.parametrize("rule", sorted(VIOLATIONS))
def test_the_rule_still_matches_its_violation(rule: str, fixture_hits: dict[str, int]) -> None:
    assert fixture_hits.get(rule, 0) > 0, (
        f"{rule} matched nothing — a silently dead rule is worse than no rule, "
        "because the scan goes green"
    )


def test_the_real_tree_is_clean(fixture_hits: dict[str, int]) -> None:
    """The rules are only worth enforcing if the codebase currently satisfies them."""
    del fixture_hits  # ordering only: the fixture proves the rules work first
    root = Path(__file__).resolve().parent.parent
    hits = _scan(root / "src")
    assert hits == {}, f"src violates its own rules: {hits}"
