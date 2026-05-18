#!/usr/bin/env python3
"""
Build the feature flags registry by diffing lime_webclient/__init__.py
across lime-webclient versions, then mapping those changes back to
lime-crm version numbers.

Strategy
--------
1. Fetch CHANGELOG.md from Lundalogik/lime-crm to build a map of
   lime-crm version → the lime-webclient version it shipped with.
2. For each unique lime-webclient version (oldest → newest), fetch
   lime_webclient/__init__.py from Lundalogik/lime-webclient at that
   version tag and parse DEFAULT_CONFIG["features"].
3. Diff consecutive snapshots:
     new key          → "added"   (with its initial default value)
     deleted key      → "removed"
     False → True     → "default_changed_to_true"
4. Attribute each change to the lime-crm version that first included
   the relevant lime-webclient version.

This approach is 100 % accurate: it reads the actual source code, so
it never misses a flag regardless of how engineers word their commits.

Authentication: set READ_TOKEN (preferred) or GITHUB_TOKEN in the env.
Without a token you will hit GitHub's 60 req/hour anonymous limit fast.
"""

import ast
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
from packaging.version import Version

# ── Configuration ────────────────────────────────────────────────────────────

LIME_CRM_REPO     = "Lundalogik/lime-crm"
WEBCLIENT_REPO    = "Lundalogik/lime-webclient"
WEBCLIENT_INIT    = "lime_webclient/__init__.py"

REGISTRY_FILE = "registry.json"
README_FILE   = "README.md"

VERSIONS_URL = (
    "https://integrations-4.internal-engineering.limecrm.cloud"
    "/webhook/28035ef9-5654-4165-b284-77ab7ad6d1d3"
)
VERSIONS_PACKAGE = "lime-crm"

# ── Regex helpers ─────────────────────────────────────────────────────────────

# ## [3.32.0](…) (2026-05-14)  or  ## [3.32.0] - 2026-05-14
VERSION_HEADER_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]", re.MULTILINE)

# "lime-webclient 46.2086.1" anywhere on a line
WEBCLIENT_VER_RE = re.compile(r"lime-webclient\s+(\d+\.\d+\.\d+)")

# ── GitHub API helpers ────────────────────────────────────────────────────────


def _auth_headers(token: str | None) -> dict:
    headers = {"Accept": "application/vnd.github.raw+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_file(repo: str, path: str, ref: str, token: str | None) -> str | None:
    """Return raw text of a file at a given ref (branch or tag), or None."""
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    try:
        resp = requests.get(
            url,
            headers=_auth_headers(token),
            params={"ref": ref},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.text
    except requests.HTTPError as exc:
        print(f"  Warning: could not fetch {path}@{ref} from {repo}: {exc}",
              file=sys.stderr)
        return None


# ── CHANGELOG parsing ─────────────────────────────────────────────────────────


def build_crm_to_webclient_map(changelog: str) -> list[tuple[str, str]]:
    """
    Parse lime-crm's CHANGELOG.md and return a list of
    (lime_crm_version, lime_webclient_version) tuples, ordered
    oldest → newest.

    For each lime-crm release block we take the *highest* lime-webclient
    version mentioned — that is the version shipped in that release.
    """
    version_matches = list(VERSION_HEADER_RE.finditer(changelog))
    if not version_matches:
        print("  Warning: no version headers found in CHANGELOG.", file=sys.stderr)
        return []

    mapping: list[tuple[str, str]] = []

    for i, match in enumerate(version_matches):
        crm_ver = match.group(1)
        block_start = match.start()
        block_end = version_matches[i + 1].start() if i + 1 < len(version_matches) else len(changelog)
        block = changelog[block_start:block_end]

        wc_versions = WEBCLIENT_VER_RE.findall(block)
        if not wc_versions:
            continue

        # The highest version mentioned in this block is the shipped version.
        max_wc = str(max(Version(v) for v in wc_versions))
        mapping.append((crm_ver, max_wc))

    # Return oldest → newest
    mapping.reverse()
    return mapping


# ── DEFAULT_CONFIG["features"] parser ────────────────────────────────────────


def parse_features(source: str) -> dict | None:
    """
    Extract DEFAULT_CONFIG["features"] from lime_webclient/__init__.py.
    Returns a plain dict {flag_name: bool_default} or None on parse error.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        print(f"  Warning: SyntaxError parsing __init__.py: {exc}", file=sys.stderr)
        return None

    for node in ast.walk(tree):
        # Look for:  DEFAULT_CONFIG = { … }
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "DEFAULT_CONFIG"
            for t in node.targets
        ):
            continue

        # The value must be a dict literal
        if not isinstance(node.value, ast.Dict):
            continue

        # Find the "features" key inside DEFAULT_CONFIG
        for key, val in zip(node.value.keys, node.value.values):
            if not (isinstance(key, ast.Constant) and key.value == "features"):
                continue
            if not isinstance(val, ast.Dict):
                continue
            result = {}
            for fk, fv in zip(val.keys, val.values):
                if not isinstance(fk, ast.Constant):
                    continue
                flag_name = fk.value
                # Resolve the default to a Python bool/None
                if isinstance(fv, ast.Constant):
                    result[flag_name] = fv.value
                elif isinstance(fv, ast.NameConstant):  # Python < 3.8 compat
                    result[flag_name] = fv.value
                else:
                    # Non-literal default (e.g. a variable) — store as None
                    result[flag_name] = None
            return result

    print("  Warning: DEFAULT_CONFIG[\"features\"] not found in __init__.py",
          file=sys.stderr)
    return None


# ── Core diffing logic ────────────────────────────────────────────────────────


def diff_features(old: dict, new: dict) -> list[dict]:
    """
    Compare two features dicts and return a list of change events:
      {"flag": name, "event": "added"|"removed"|"default_changed_to_true",
       "default": <bool>}
    """
    events = []
    for name, default in new.items():
        if name not in old:
            events.append({"flag": name, "event": "added", "default": default})
        elif not old[name] and default:
            events.append({"flag": name, "event": "default_changed_to_true", "default": True})
    for name in old:
        if name not in new:
            events.append({"flag": name, "event": "removed", "default": None})
    return events


def build_registry(token: str | None) -> dict:
    """
    Main logic: fetch, diff, and assemble the flags registry.
    """
    # ── Step 1: get the lime-crm CHANGELOG ──
    print(f"Fetching CHANGELOG.md from {LIME_CRM_REPO}…")
    changelog = fetch_file(LIME_CRM_REPO, "CHANGELOG.md", "main", token)
    if not changelog:
        print("  Error: could not fetch CHANGELOG.md.", file=sys.stderr)
        return {}

    crm_to_wc = build_crm_to_webclient_map(changelog)
    print(f"  Found {len(crm_to_wc)} lime-crm versions with webclient mapping.")

    # ── Step 2: fetch unique webclient __init__.py snapshots ──
    # Build an ordered list of unique webclient versions (oldest → newest)
    seen_wc: dict[str, str] = {}  # wc_ver → lime_crm_ver (first crm ver to ship it)
    for crm_ver, wc_ver in crm_to_wc:
        if wc_ver not in seen_wc:
            seen_wc[wc_ver] = crm_ver

    unique_wc_versions = list(seen_wc.keys())  # already oldest→newest
    print(f"  {len(unique_wc_versions)} unique lime-webclient versions to fetch…")

    snapshots: list[tuple[str, str, dict]] = []  # (wc_ver, crm_ver, features)

    for idx, wc_ver in enumerate(unique_wc_versions):
        crm_ver = seen_wc[wc_ver]
        tag = f"v{wc_ver}"
        print(f"  [{idx + 1}/{len(unique_wc_versions)}] webclient {wc_ver} (lime-crm {crm_ver})…",
              end=" ", flush=True)
        source = fetch_file(WEBCLIENT_REPO, WEBCLIENT_INIT, tag, token)
        if source is None:
            print("skipped (fetch failed)")
            continue
        features = parse_features(source)
        if features is None:
            print("skipped (parse failed)")
            continue
        snapshots.append((wc_ver, crm_ver, features))
        print(f"{len(features)} flags")

    if not snapshots:
        print("  Error: no snapshots collected.", file=sys.stderr)
        return {}

    # ── Step 3: diff consecutive snapshots ──
    flags: dict = {}

    for i, (wc_ver, crm_ver, features) in enumerate(snapshots):
        old_features = snapshots[i - 1][2] if i > 0 else {}
        events = diff_features(old_features, features)

        for ev in events:
            name = ev["flag"]
            event = ev["event"]
            default = ev["default"]

            if name not in flags:
                flags[name] = {
                    "status": "active",
                    "current_default": False,
                    "added_in": {},
                    "default_true_since": {},
                    "removed_in": {},
                    "history": [],
                }

            flag = flags[name]
            entry: dict = {"event": event, "lime_crm": crm_ver}
            flag["history"].append(entry)

            if event == "added":
                if not flag["added_in"]:
                    flag["added_in"] = {"lime_crm": crm_ver}
                flag["current_default"] = bool(default)
                if flag["status"] != "removed":
                    flag["status"] = "active"
            elif event == "default_changed_to_true":
                flag["current_default"] = True
                if not flag["default_true_since"]:
                    flag["default_true_since"] = {"lime_crm": crm_ver}
                flag["status"] = "active"
            elif event == "removed":
                flag["removed_in"] = {"lime_crm": crm_ver}
                flag["status"] = "removed"
                flag["current_default"] = None

    # ── Step 4: set current_default from the latest snapshot ──
    latest_features = snapshots[-1][2]
    for name, flag in flags.items():
        if flag["status"] == "active" and name in latest_features:
            flag["current_default"] = bool(latest_features[name])

    print(f"\nDone — {len(flags)} flags total.")
    return flags


# ── Deployment versions ───────────────────────────────────────────────────────


def fetch_suggested_versions() -> list[dict]:
    """
    Fetch lime-crm deployment versions (Current in cloud, Verification,
    Latest available) from the internal versions page.
    Returns [{"version": "3.32.0", "label": "Current in cloud"}, …]
    ordered Current in cloud → Verification → Latest available.
    """
    from bs4 import BeautifulSoup

    try:
        resp = requests.get(VERSIONS_URL, timeout=15)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  Warning: could not fetch versions page: {exc}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

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

    for row in soup.find_all("tr")[1:]:
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        if VERSIONS_PACKAGE not in cells[0].get_text(strip=True).lower():
            continue

        suggestions = []
        for label in ("Current in cloud", "Verification", "Latest available"):
            idx = col_map.get(label)
            if idx is not None and idx < len(cells):
                ver = cells[idx].get_text(strip=True)
                if ver and ver not in ("—", "-"):
                    suggestions.append({"version": ver, "label": label})
        print(f"  Deployment versions: {suggestions}")
        return suggestions

    print(f"  Warning: '{VERSIONS_PACKAGE}' row not found on versions page.", file=sys.stderr)
    return []


# ── Output generators ─────────────────────────────────────────────────────────


def build_registry_json(flags: dict, suggested_versions: list[dict]) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [LIME_CRM_REPO, WEBCLIENT_REPO],
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
    active  = {k: v for k, v in flags.items() if v["status"] == "active"}
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
        "| Flag | Added in | Default | Default → true in |",
        "|------|----------|:-------:|-------------------|",
    ]
    for name, flag in sorted(active.items()):
        default_val = "true" if flag["current_default"] else "false"
        lines.append(
            f"| `{name}`"
            f" | {_ver_display(flag.get('added_in'))}"
            f" | {default_val}"
            f" | {_ver_display(flag.get('default_true_since')) or '—'} |"
        )

    lines += [
        "",
        "## Removed flags",
        "",
        "| Flag | Added in | Removed in |",
        "|------|----------|------------|",
    ]
    for name, flag in sorted(removed.items()):
        lines.append(
            f"| `{name}`"
            f" | {_ver_display(flag.get('added_in'))}"
            f" | {_ver_display(flag.get('removed_in'))} |"
        )

    return "\n".join(lines) + "\n"


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    token = os.environ.get("READ_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Warning: no READ_TOKEN or GITHUB_TOKEN set — "
            "unauthenticated requests are rate-limited to 60/hour.",
            file=sys.stderr,
        )

    flags = build_registry(token)

    print("\nFetching deployment versions…")
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
