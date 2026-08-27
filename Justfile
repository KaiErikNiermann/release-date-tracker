set shell := ["bash", "-eu", "-o", "pipefail", "-c"]

# List recipes
default:
    @just --list

# --- Development setup ---

# Install dependencies and wire up the git hooks
dev:
    poetry install
    git config core.hooksPath .githooks
    @echo "Development environment ready — 'just check' runs what CI runs."

# --- Checks ---

# Lint
lint:
    poetry run ruff check src tests

# Project rules: conventions a matcher can judge, and the shared helpers not to re-handroll
semgrep:
    poetry run semgrep --config .semgrep/ --error --quiet src tests

# Prove every rule still matches something — a pattern that silently stops matching is
# indistinguishable from a clean tree, which is how half of these shipped broken once.
semgrep-selftest:
    poetry run pytest tests/test_semgrep_rules.py -q

# Fix what ruff can fix
lint-fix:
    poetry run ruff check --fix src tests

# Check formatting
fmt-check:
    poetry run ruff format --check src tests

# Format
fmt:
    poetry run ruff format src tests

# Type-check (strict)
typecheck:
    poetry run pyright src tests

# Run the test suite
test *ARGS:
    poetry run pytest {{ARGS}}

# Cyclomatic complexity — flag anything below grade B (see CLAUDE.md)
complexity:
    poetry run radon cc src -nb --total-average

# Everything CI runs
check: lint semgrep fmt-check typecheck test

# --- Running ---

# Interactive browser
tui:
    poetry run rdt tui

# Any rdt subcommand, e.g. `just rdt show --json`
rdt *ARGS:
    poetry run rdt {{ARGS}}

# Where this machine's tracker actually lives
paths:
    @poetry run python -c "from release_tracker.config import get_settings as g; s=g(); print(f'db      {s.db_path}\ncache   {s.trend_cache_path}\nseeds   {s.seeds_path}')"

# --- Packaging ---

# Build the wheel + sdist into dist/
build:
    poetry build

# Install the built wheel into the current user's pipx environment
install-local: build
    #!/usr/bin/env bash
    set -euo pipefail
    wheel=$(ls -t dist/*.whl | head -1)
    pipx install --force "$wheel"
    echo "Installed $wheel — 'rdt --help' should now work from anywhere."

# Remove build artifacts and caches
clean:
    rm -rf dist build .pytest_cache .ruff_cache
    find . -name __pycache__ -type d -prune -exec rm -rf {} +

# --- Versioning & Release ---

# Show current version
version:
    @grep '^version' pyproject.toml | head -1 | sed 's/version = "\(.*\)"/\1/'

# Bump version, commit, tag, push, create the GitHub release
release bump="patch":
    #!/usr/bin/env bash
    set -euo pipefail
    current=$(just version)
    IFS='.' read -r major minor patch <<< "$current"
    case "{{bump}}" in
        major) major=$((major + 1)); minor=0; patch=0 ;;
        minor) minor=$((minor + 1)); patch=0 ;;
        patch) patch=$((patch + 1)) ;;
        *) echo "Invalid bump type: {{bump}} (use major, minor, or patch)"; exit 1 ;;
    esac
    just _release "$major.$minor.$patch"

# Release with an explicit version
release-version version:
    @just _release "{{version}}"

# Re-tag HEAD and re-trigger the release workflow for an existing version
rerun version:
    #!/usr/bin/env bash
    set -euo pipefail
    version=$(just _normalize-version "{{version}}")
    git push
    git tag -d "v$version" 2>/dev/null || true
    git push --delete origin "v$version" 2>/dev/null || true
    git tag "v$version"
    git push origin "v$version"
    echo "Re-triggered release workflow for v$version"

# Delete and recreate the GitHub release + retag HEAD
rerelease version:
    #!/usr/bin/env bash
    set -euo pipefail
    version=$(just _normalize-version "{{version}}")
    gh release delete "v$version" -y 2>/dev/null || true
    just rerun "$version"

# Internal: sync the version, commit, tag, push — the tag is what builds the release
_release version:
    #!/usr/bin/env bash
    set -euo pipefail
    version=$(just _normalize-version "{{version}}")
    just check
    just _sync-versions "$version"
    poetry lock
    git add pyproject.toml poetry.lock
    git commit -m "chore(release): v$version"
    git push
    git tag "v$version"
    git push origin "v$version"
    echo "Tagged v$version — GitHub Actions builds the artifacts and cuts the release."

# Internal: update the version wherever it is written
_sync-versions version:
    #!/usr/bin/env bash
    set -euo pipefail
    sed -i "0,/^version = .*/s//version = \"{{version}}\"/" pyproject.toml
    echo "Synced version to v{{version}}"

# Internal: strip an optional leading 'v' and validate X.Y.Z (prints normalized)
_normalize-version version:
    #!/usr/bin/env bash
    set -euo pipefail
    v="{{version}}"
    v="${v#v}"  # tolerate a v-prefixed arg without producing a 'vv' tag
    if [[ ! "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "error: invalid version '{{version}}' — expected X.Y.Z (a leading 'v' is allowed)" >&2
        exit 1
    fi
    printf '%s' "$v"

# Wait for the release workflow to finish
wait-release:
    #!/usr/bin/env bash
    set -euo pipefail
    run_id=$(gh run list --workflow "Release" --limit 1 --json databaseId -q '.[0].databaseId')
    gh run watch "$run_id" --exit-status && echo "Release workflow succeeded"
