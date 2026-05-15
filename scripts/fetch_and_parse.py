#!/usr/bin/env python3
"""
Fetch GitHub release notes for configured source repos and parse feature flag
events. Outputs registry.json and README.md.

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
from packaging.version import Version, InvalidVersion

# ── Configuration ────────────────────────────────────────────────────────────

SOURCE_REPOS = [
    {
        "repo": "Lundalogik/lime-crm",
        "version_key": "lime_crm",
        "label": "Lime CRM",
    },
    {
        "repo": "Lundalogik/limepkg-email",
        "version_key": "limepkg_email",
        "label": "limepkg-email",
    },
]

REGISTRY_FILE = "registry.json"
README_FILE = "README.md"

# ── Regex patterns ───────────────────────────────────────────────────────────

# Lines that mention a feature flag or switch
FLAG_LINE_RE = re.compile(
    r"feature\s*(?:flag|switch)|featureswitch",
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

# Event classification
REMOVE_RE = re.compile(r"\b(remov|delet|drop|retir)\w*\b", re.IGNORECASE)
DEFAULT_TRUE_RE = re.compile(
    r"(enable\w*\s+.*\bby\s+default"
    r"|flip\s+feature"
    r"|default\w*\s+to\s+true"
    r"|set.*default.*true)",
    re.IGNORECASE,
)
ADD_RE = re.compile(r"\b(add|introduc|implement|creat|new)\w*\b", re.IGNORECASE)

# ── GitHub API helper ────────────────────────────────────────────────────────


def github_get_all(path: str, token: str | None) -> list:
    """Fetch all pages from a GitHub API list endpoint."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    results: list = []
    url = f"https://api.github.com/{path.lstrip('/')}"
    while url:
        resp = requests.get(url, headers=headers, params={"per_page": 100}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return [data]
        url = (resp.links.get("next") or {}).get("url")
    return results


# ── Parsing helpers ──────────────────────────────────────────────────────────


def classify_event(line: str) -> str | None:
    if REMOVE_RE.search(line):
        return "removed"
    if DEFAULT_TRUE_RE.search(line):
        return "default_changed_to_true"
    if ADD_RE.search(line):
        return "added"
    return None


def extract_flag_name(line: str) -> str | None:
    m = FLAG_BACKTICK_RE.search(line)
    if m:
        return m.group(1)
    m = FLAG_NAMED_RE.search(line)
    if m:
        return m.group(1)
    return None


def extract_source_package(line: str) -> str | None:
    m = SOURCE_PKG_RE.search(line)
    return m.group(1) if m else None


def safe_version(v: str) -> Version | None:
    try:
        return Version(v.lstrip("v"))
    except InvalidVersion:
        return None


# ── Core logic ───────────────────────────────────────────────────────────────


def fetch_releases(repo_slug: str, token: str | None) -> list[dict]:
    """Return published (non-draft, non-prerelease) releases, oldest first."""
    releases = github_get_all(f"repos/{repo_slug}/releases", token)
    releases = [r for r in releases if not r.get("draft") and not r.get("prerelease")]
    releases.sort(key=lambda r: r.get("published_at", ""))
    return releases


def parse_release_body(
    body: str,
    version_str: str,
    version_key: str,
    flags: dict,
) -> None:
    """Scan one release body for feature flag events; update flags in-place."""
    if not body:
        return

    for line in body.splitlines():
        if not FLAG_LINE_RE.search(line):
            continue

        event = classify_event(line)
        flag_name = extract_flag_name(line)
        source_pkg = extract_source_package(line)

        if not flag_name or not event:
            continue

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


def build_registry(token: str | None) -> dict:
    flags: dict = {}
    for source in SOURCE_REPOS:
        repo_slug = source["repo"]
        version_key = source["version_key"]
        print(f"Fetching releases for {repo_slug}…")
        try:
            releases = fetch_releases(repo_slug, token)
        except requests.HTTPError as exc:
            print(f"  Warning: {exc}", file=sys.stderr)
            continue
        print(f"  {len(releases)} releases found")
        for release in releases:
            version_str = release["tag_name"].lstrip("v")
            parse_release_body(
                release.get("body") or "",
                version_str,
                version_key,
                flags,
            )
    return flags


# ── Output generators ────────────────────────────────────────────────────────


def build_registry_json(flags: dict) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [s["repo"] for s in SOURCE_REPOS],
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

    registry = build_registry_json(flags)
    with open(REGISTRY_FILE, "w") as fh:
        json.dump(registry, fh, indent=2)
    print(f"Wrote {REGISTRY_FILE} ({len(flags)} flags)")

    readme = build_readme(flags)
    with open(README_FILE, "w") as fh:
        fh.write(readme)
    print(f"Wrote {README_FILE}")


if __name__ == "__main__":
    main()
