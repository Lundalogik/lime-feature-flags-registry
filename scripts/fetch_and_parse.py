#!/usr/bin/env python3
"""
Fetch CHANGELOG.md for each configured source repo via the GitHub API and
parse feature flag events. Outputs registry.json and README.md.

Authentication: set READ_TOKEN (preferred) or GITHUB_TOKEN in the environment.
For public repos no token is required, but you will hit rate limits quickly
without one.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone

import requests

# ── Configuration ────────────────────────────────────────────────────────────

SOURCE_REPOS = [
    {
        "repo": "Lundalogik/lime-crm",
        "version_key": "lime_crm",
        "label": "Lime CRM",
        "changelog_path": "CHANGELOG.md",
        "changelog_branch": "main",
    },
]

REGISTRY_FILE = "registry.json"
README_FILE = "README.md"

VERSIONS_URL = (
    "https://integrations-4.internal-engineering.limecrm.cloud"
    "/webhook/28035ef9-5654-4165-b284-77ab7ad6d1d3"
)
VERSIONS_PACKAGE = "lime-crm"

# ── Regex patterns ───────────────────────────────────────────────────────────

# Version header in CHANGELOG.md:
#   ## [3.32.0](https://...) (2026-05-14)   ← semantic-release format
#   ## [3.32.0] - 2026-05-14                ← keep-a-changelog format
VERSION_HEADER_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)

# Lines that mention a feature flag or switch.
# Covers both natural language ("feature switch foo") and the conventional
# commit scope format ("**feature-switches:** add `foo`").
FLAG_LINE_RE = re.compile(
    r"feature[-\s]?switches?|feature\s*(?:flag|switch)|featureswitch",
    re.IGNORECASE,
)

# Extract flag name — try in order of confidence
FLAG_BACKTICK_RE = re.compile(r"`([a-zA-Z][a-zA-Z0-9_]+)`")
FLAG_NAMED_RE = re.compile(
    r"\b(use[A-Z][a-zA-Z0-9]+|limepkg[A-Z][a-zA-Z0-9]+"
    r"|use_[a-z][a-z0-9_]+|run_[a-z][a-z0-9_]+)\b"
)

# Source package from a GitHub commit URL on the same line
SOURCE_PKG_RE = re.compile(r"github\.com/[^/]+/([^/]+)/commit/")

# Event classification — ordered from most to least specific
REMOVE_RE = re.compile(r"\b(remov|delet|drop|retir)\w*\b", re.IGNORECASE)
# "enable … by default" but NOT "enable in examples" / "enable in tests"
DEFAULT_TRUE_RE = re.compile(
    r"(enable\w*\s+.*\bby\s+default"
    r"|flip\s+feature"
    r"|default\w*\s+to\s+true"
    r"|set.*default.*true)",
    re.IGNORECASE,
)
ADD_RE = re.compile(r"\b(add|introduc|implement|creat|new)\w*\b", re.IGNORECASE)
FEATURE_SWITCHES_SCOPE_RE = re.compile(r"feature-switches?:", re.IGNORECASE)

# ── GitHub API helpers ────────────────────────────────────────────────────────


def _auth_headers(token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github.raw+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_changelog(
    repo_slug: str, path: str, branch: str, token: str | None
) -> str | None:
    """Return the raw text of a CHANGELOG.md from GitHub, or None on error."""
    url = f"https://api.github.com/repos/{repo_slug}/contents/{path}"
    try:
        resp = requests.get(
            url,
            headers=_auth_headers(token),
            params={"ref": branch},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text
    except requests.HTTPError as exc:
        print(f"  Warning: could not fetch {path} from {repo_slug}: {exc}", file=sys.stderr)
        return None


# ── Parsing helpers ───────────────────────────────────────────────────────────


def classify_event(line: str) -> str | None:
    if REMOVE_RE.search(line):
        return "removed"
    if DEFAULT_TRUE_RE.search(line):
        return "default_changed_to_true"
    if ADD_RE.search(line):
        return "added"
    # "**feature-switches:** useFoo" with no verb → infer "added"
    # (removals always contain "remove", caught above)
    if FEATURE_SWITCHES_SCOPE_RE.search(line):
        return "added"
    return None


def extract_flag_names(line: str) -> list[str]:
    """Return ALL flag names found in a line (a single line can mention several)."""
    names = FLAG_BACKTICK_RE.findall(line)
    if names:
        return names
    return FLAG_NAMED_RE.findall(line)


def extract_source_package(line: str) -> str | None:
    m = SOURCE_PKG_RE.search(line)
    return m.group(1) if m else None


# ── Core logic ───────────────────────────────────────────────────────────────


def parse_changelog_content(
    content: str, version_key: str, flags: dict
) -> None:
    """
    Split a CHANGELOG.md into per-version blocks (newest first) and scan each
    block for feature flag events. Updates flags in-place.
    """
    # Find all version headers and their positions
    version_matches = list(VERSION_HEADER_RE.finditer(content))
    if not version_matches:
        print("  Warning: no version headers found in changelog.", file=sys.stderr)
        return

    # Reverse so we process oldest → newest (important for correct history order)
    version_matches = list(reversed(version_matches))

    for i, match in enumerate(version_matches):
        version_str = match.group(1)
        block_start = match.start()
        # The block ends where the next (newer) version starts
        block_end = version_matches[i - 1].start() if i > 0 else len(content)
        block = content[block_start:block_end]

        for line in block.splitlines():
            # Path A: line explicitly mentions "feature switch/flag" or
            #         "feature-switches:" — covers add/remove/default events.
            # Path B: line has "enable … by default" / "set default true" with
            #         a flag name — catches default changes that don't use the
            #         feature-switches scope (e.g. "enable `useFoo` by default").
            # Path C: line has a flag name + remove keyword without "feature
            #         switch" text (e.g. "remove useRecentlyDeleted").
            is_flag_line = FLAG_LINE_RE.search(line)
            is_default_line = DEFAULT_TRUE_RE.search(line)
            is_remove_line = REMOVE_RE.search(line)

            if not is_flag_line and not is_default_line and not is_remove_line:
                continue

            event = classify_event(line)
            flag_names = extract_flag_names(line)
            source_pkg = extract_source_package(line)

            if not flag_names or not event:
                continue

            for flag_name in flag_names:
                if flag_name not in flags:
                    flags[flag_name] = {
                        "source_package": source_pkg,
                        "status": "active",
                        "current_default": False,
                        "added_in": {},
                        "default_true_since": {},
                        "removed_in": {},
                        "history": [],
                    }

                flag = flags[flag_name]

                if source_pkg and not flag["source_package"]:
                    flag["source_package"] = source_pkg

                entry: dict = {"event": event, version_key: version_str}
                if source_pkg:
                    entry["source_package"] = source_pkg
                flag["history"].append(entry)

                if event == "added":
                    if not flag["added_in"]:
                        flag["added_in"] = {version_key: version_str}
                    if flag["status"] != "removed":
                        flag["status"] = "active"
                elif event == "default_changed_to_true":
                    flag["current_default"] = True
                    if not flag["default_true_since"]:
                        flag["default_true_since"] = {version_key: version_str}
                    flag["status"] = "active"
                elif event == "removed":
                    flag["removed_in"] = {version_key: version_str}
                    flag["status"] = "removed"
                    flag["current_default"] = None


def fetch_suggested_versions() -> list[dict]:
    """
    Fetch lime-crm deployment versions (Current in cloud, Verification,
    Latest available) from the internal versions page.
    Returns a list like [{"version": "3.32.0", "label": "Latest available"}, …]
    ordered newest → oldest.
    """
    from bs4 import BeautifulSoup

    try:
        resp = requests.get(VERSIONS_URL, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  Warning: could not fetch versions page: {exc}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the header row to determine column indices
    header_row = soup.find("tr")
    if not header_row:
        print("  Warning: no table rows found on versions page.", file=sys.stderr)
        return []

    headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
    col_map = {}
    for label, keywords in {
        "Current in cloud": ["current in cloud", "current"],
        "Verification":     ["verification"],
        "Latest available": ["latest available", "latest"],
    }.items():
        for i, h in enumerate(headers):
            if any(k in h for k in keywords):
                col_map[label] = i
                break

    # Find the lime-crm row
    for row in soup.find_all("tr")[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        if VERSIONS_PACKAGE not in cells[0].get_text(strip=True).lower():
            continue

        suggestions = []
        for label in ("Latest available", "Verification", "Current in cloud"):
            idx = col_map.get(label)
            if idx is not None and idx < len(cells):
                ver = cells[idx].get_text(strip=True)
                if ver and ver != "—" and ver != "-":
                    suggestions.append({"version": ver, "label": label})
        print(f"  Deployment versions: {suggestions}")
        return suggestions

    print(f"  Warning: '{VERSIONS_PACKAGE}' row not found on versions page.", file=sys.stderr)
    return []


def build_registry(token: str | None) -> dict:
    flags: dict = {}
    for source in SOURCE_REPOS:
        repo_slug = source["repo"]
        version_key = source["version_key"]
        print(f"Fetching CHANGELOG.md from {repo_slug}…")
        content = fetch_changelog(
            repo_slug,
            source["changelog_path"],
            source["changelog_branch"],
            token,
        )
        if content is None:
            continue
        print(f"  Parsing {len(content):,} characters…")
        parse_changelog_content(content, version_key, flags)
        print(f"  Done — {len(flags)} flags total so far")
    return flags


# ── Output generators ────────────────────────────────────────────────────────


def build_registry_json(flags: dict, suggested_versions: list[dict]) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [s["repo"] for s in SOURCE_REPOS],
        "suggested_versions": suggested_versions,
        "flags": flags,
    }


def _ver_display(version_dict: dict) -> str:
    if not version_dict:
        return "—"
    return ", ".join(
        f"{k.replace('_', '-')} {v}" for k, v in version_dict.items()
    )


def build_readme(flags: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    active = {k: v for k, v in flags.items() if v["status"] == "active"}
    removed = {k: v for k, v in flags.items() if v["status"] == "removed"}

    lines = [
        "# Lime Feature Flags Registry",
        "",
        f"*Last updated: {now} &nbsp;·&nbsp; {len(flags)} flags tracked*",
        "",
        "Use the **[version lookup tool](https://lundalogik.github.io/lime-feature-flags-registry/)**"
        " to see exactly which flags apply to your installation.",
        "",
        "---",
        "",
        "## Active flags",
        "",
        "| Flag | Package | Added in | Default | Default → true in |",
        "|------|---------|----------|:-------:|-------------------|",
    ]
    for name, flag in sorted(active.items()):
        default_val = "true" if flag["current_default"] else "false"
        lines.append(
            f"| `{name}` | {flag.get('source_package') or '—'}"
            f" | {_ver_display(flag.get('added_in'))} | {default_val}"
            f" | {_ver_display(flag.get('default_true_since')) or '—'} |"
        )

    lines += [
        "",
        "## Removed flags",
        "",
        "| Flag | Package | Added in | Removed in |",
        "|------|---------|----------|------------|",
    ]
    for name, flag in sorted(removed.items()):
        lines.append(
            f"| `{name}` | {flag.get('source_package') or '—'}"
            f" | {_ver_display(flag.get('added_in'))}"
            f" | {_ver_display(flag.get('removed_in'))} |"
        )

    return "\n".join(lines) + "\n"


# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    token = os.environ.get("READ_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Warning: no READ_TOKEN or GITHUB_TOKEN set — "
            "unauthenticated requests are rate-limited to 60/hour.",
            file=sys.stderr,
        )

    flags = build_registry(token)

    print("Fetching deployment versions…")
    suggested_versions = fetch_suggested_versions()

    registry = build_registry_json(flags, suggested_versions)
    with open(REGISTRY_FILE, "w") as fh:
        json.dump(registry, fh, indent=2)
    print(f"Wrote {REGISTRY_FILE} ({len(flags)} flags)")

    readme = build_readme(flags)
    with open(README_FILE, "w") as fh:
        fh.write(readme)
    print(f"Wrote {README_FILE}")


if __name__ == "__main__":
    main()
