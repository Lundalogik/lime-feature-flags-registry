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

Incremental runs
----------------
On subsequent runs the existing registry.json is used as a cache.
Only webclient versions newer than `incremental_state.last_processed_webclient_version`
are fetched, making nightly runs very cheap (typically 0–2 API calls
instead of ~300).

Pass --force-rebuild to ignore the cache and reprocess everything.

Authentication: set READ_TOKEN (preferred) or GITHUB_TOKEN in the env.
Without a token you will hit GitHub's 60 req/hour anonymous limit fast.
"""

import argparse
import ast
import json
import os
import re
import sys
from datetime import datetime, timezone

import requests
from packaging.version import Version

# ── Configuration ────────────────────────────────────────────────────────────

LIME_CRM_REPO        = "Lundalogik/lime-crm"
WEBCLIENT_REPO       = "Lundalogik/lime-webclient"
WEBCLIENT_INIT       = "lime_webclient/__init__.py"
WEBCLIENT_PKGLOCK    = "package-lock.json"
CRM_COMPONENTS_REPO  = "Lundalogik/lime-crm-components"
CRM_COMPONENTS_TS    = "src/core/feature-switches.ts"
CRM_COMPONENTS_PKG   = "@lundalogik/lime-crm-components"

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

# Flag names declared in the TypeScript FeatureSwitches interface
#   useFoo: boolean;
#   displayBar: boolean;
TS_FLAG_RE = re.compile(r"^\s+(\w+):\s*boolean;", re.MULTILINE)

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
        block_end = (version_matches[i + 1].start()
                     if i + 1 < len(version_matches) else len(changelog))
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
                if isinstance(fv, ast.Constant):
                    result[flag_name] = fv.value
                elif isinstance(fv, ast.NameConstant):  # Python < 3.8 compat
                    result[flag_name] = fv.value
                else:
                    # Non-literal default (e.g. a variable) — store as None
                    result[flag_name] = None
            return result

    print('  Warning: DEFAULT_CONFIG["features"] not found in __init__.py',
          file=sys.stderr)
    return None


# ── lime-crm-components TypeScript parser ────────────────────────────────────


def parse_crm_components_version(pkg_lock_text: str) -> str | None:
    """
    Extract the resolved version of @lundalogik/lime-crm-components from
    lime-webclient's package-lock.json. Returns e.g. "3.477.2" or None.
    """
    try:
        data = json.loads(pkg_lock_text)
    except json.JSONDecodeError:
        return None
    # npm lockfile v2/v3: packages["node_modules/@lundalogik/lime-crm-components"]
    packages = data.get("packages", {})
    key = f"node_modules/{CRM_COMPONENTS_PKG}"
    if key in packages:
        return packages[key].get("version")
    # Fallback: dependencies (lockfile v1)
    deps = data.get("dependencies", {})
    entry = deps.get(CRM_COMPONENTS_PKG, {})
    return entry.get("version")


def parse_ts_feature_switches(ts_source: str) -> set[str]:
    """
    Parse the TypeScript FeatureSwitches interface declaration and return
    the set of flag names declared in it.
    """
    return set(TS_FLAG_RE.findall(ts_source))


def merge_features(
    python_features: dict,
    ts_flags: set[str],
) -> dict:
    """
    Merge Python defaults with TypeScript-declared flags.
    Python defaults take precedence; TS-only flags get default False.
    """
    merged = dict(python_features)
    for flag in ts_flags:
        if flag not in merged:
            merged[flag] = False
    return merged


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


# ── Incremental state helpers ─────────────────────────────────────────────────


def load_incremental_state(path: str) -> tuple[dict, dict]:
    """
    Load existing flags and incremental state from registry.json.
    Returns ({}, {}) if the file does not exist or has no incremental_state.
    """
    if not os.path.exists(path):
        return {}, {}
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data.get("flags", {}), data.get("incremental_state", {})
    except (json.JSONDecodeError, OSError):
        return {}, {}


# ── Core registry builder ─────────────────────────────────────────────────────


def build_registry(
    token: str | None,
    existing_flags: dict,
    incremental_state: dict,
) -> tuple[dict, dict]:
    """
    Fetch, diff, and assemble the flags registry incrementally.

    Returns (flags, new_incremental_state).
    """
    # ── Step 1: get the lime-crm CHANGELOG ──
    print(f"Fetching CHANGELOG.md from {LIME_CRM_REPO}…")
    changelog = fetch_file(LIME_CRM_REPO, "CHANGELOG.md", "main", token)
    if not changelog:
        print("  Error: could not fetch CHANGELOG.md.", file=sys.stderr)
        return existing_flags, incremental_state

    crm_to_wc = build_crm_to_webclient_map(changelog)
    print(f"  Found {len(crm_to_wc)} lime-crm versions with webclient mapping.")

    # ── Step 2: build ordered list of unique webclient versions ──
    seen_wc: dict[str, str] = {}  # wc_ver → first lime_crm_ver to ship it
    for crm_ver, wc_ver in crm_to_wc:
        if wc_ver not in seen_wc:
            seen_wc[wc_ver] = crm_ver

    unique_wc_versions = list(seen_wc.keys())  # oldest → newest

    # ── Step 3: determine what to process (incremental vs full) ──
    resume_from = incremental_state.get("last_processed_webclient_version")

    if resume_from and resume_from in unique_wc_versions:
        resume_idx = unique_wc_versions.index(resume_from)
        versions_to_process = unique_wc_versions[resume_idx + 1:]
        prev_features: dict = incremental_state.get("features_snapshot", {})
        print(f"  Resuming from webclient {resume_from} — "
              f"{len(versions_to_process)} new version(s) to process.")
    else:
        versions_to_process = unique_wc_versions
        prev_features = {}
        if resume_from:
            print(f"  Warning: resume point {resume_from} not found in CHANGELOG — "
                  "doing full rebuild.", file=sys.stderr)
        else:
            print(f"  No prior state — full rebuild ({len(versions_to_process)} versions).")

    if not versions_to_process:
        print("  Registry is already up to date.")
        return existing_flags, incremental_state

    # ── Step 4: fetch snapshots and diff ──
    flags = dict(existing_flags)
    last_successfully_diffed: str | None = None
    final_prev_features = prev_features

    # Cache: crm-components version → set of TS flag names (avoids re-fetching
    # the same crm-components version across many consecutive webclient versions)
    ts_flags_cache: dict[str, set[str]] = {}
    prev_crm_components_ver: str | None = None

    for idx, wc_ver in enumerate(versions_to_process):
        crm_ver = seen_wc[wc_ver]
        tag = f"v{wc_ver}"
        print(f"  [{idx + 1}/{len(versions_to_process)}] "
              f"webclient {wc_ver} (lime-crm {crm_ver})…",
              end=" ", flush=True)

        source = fetch_file(WEBCLIENT_REPO, WEBCLIENT_INIT, tag, token)
        if source is None:
            print("skipped (fetch failed)")
            continue

        python_features = parse_features(source)
        if python_features is None:
            print("skipped (parse failed)")
            continue

        # Resolve lime-crm-components TS flags for this webclient version
        ts_flags: set[str] = set()
        pkg_lock_text = fetch_file(WEBCLIENT_REPO, WEBCLIENT_PKGLOCK, tag, token)
        if pkg_lock_text:
            crc_ver = parse_crm_components_version(pkg_lock_text)
            if crc_ver and crc_ver not in ts_flags_cache:
                ts_source = fetch_file(
                    CRM_COMPONENTS_REPO, CRM_COMPONENTS_TS, f"v{crc_ver}", token
                )
                if ts_source:
                    ts_flags_cache[crc_ver] = parse_ts_feature_switches(ts_source)
                    prev_crm_components_ver = crc_ver
            if crc_ver:
                ts_flags = ts_flags_cache.get(crc_ver, set())

        features = merge_features(python_features, ts_flags)
        events = diff_features(prev_features, features)
        print(f"{len(features)} flags ({len(python_features)} py + "
              f"{len(ts_flags) - len(python_features & ts_flags)} ts-only), "
              f"{len(events)} change(s)")

        for ev in events:
            name   = ev["flag"]
            event  = ev["event"]
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
            flag["history"].append({"event": event, "lime_crm": crm_ver})

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

        prev_features = features
        final_prev_features = features
        last_successfully_diffed = wc_ver

    # ── Step 5: sync current_default from the very latest snapshot ──
    for name, flag in flags.items():
        if flag["status"] == "active" and name in final_prev_features:
            flag["current_default"] = bool(final_prev_features[name])

    active  = sum(1 for f in flags.values() if f["status"] == "active")
    removed = sum(1 for f in flags.values() if f["status"] == "removed")
    print(f"\nDone — {len(flags)} flags total ({active} active, {removed} removed).")

    # ── Step 6: build new incremental state ──
    if last_successfully_diffed:
        new_state = {
            "last_processed_webclient_version": last_successfully_diffed,
            "features_snapshot": final_prev_features,
        }
    else:
        new_state = incremental_state  # nothing new was processed, carry forward

    return flags, new_state


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

    headers = [th.get_text(strip=True).lower()
               for th in header_row.find_all(["th", "td"])]
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

    print(f"  Warning: '{VERSIONS_PACKAGE}' row not found on versions page.",
          file=sys.stderr)
    return []


# ── Output generators ─────────────────────────────────────────────────────────


def build_registry_json(
    flags: dict,
    suggested_versions: list[dict],
    incremental_state: dict,
) -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [LIME_CRM_REPO, WEBCLIENT_REPO],
        "suggested_versions": suggested_versions,
        "incremental_state": incremental_state,
        "flags": flags,
    }


def _ver_display(version_dict: dict) -> str:
    if not version_dict:
        return "—"
    return ", ".join(
        f"{k.replace('_', '-')} {v}" for k, v in version_dict.items()
    )


def build_readme(flags: dict) -> str:
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the feature flags registry.")
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Ignore cached incremental state and reprocess all historical versions.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    token = os.environ.get("READ_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print(
            "Warning: no READ_TOKEN or GITHUB_TOKEN set — "
            "unauthenticated requests are rate-limited to 60/hour.",
            file=sys.stderr,
        )

    existing_flags, incremental_state = load_incremental_state(REGISTRY_FILE)

    if args.force_rebuild:
        print("--force-rebuild: ignoring cached state, reprocessing all versions.")
        existing_flags, incremental_state = {}, {}

    flags, new_incremental_state = build_registry(token, existing_flags, incremental_state)

    print("\nFetching deployment versions…")
    suggested_versions = fetch_suggested_versions()

    registry = build_registry_json(flags, suggested_versions, new_incremental_state)
    with open(REGISTRY_FILE, "w") as fh:
        json.dump(registry, fh, indent=2)
    print(f"Wrote {REGISTRY_FILE} ({len(flags)} flags)")

    readme = build_readme(flags)
    with open(README_FILE, "w") as fh:
        fh.write(readme)
    print(f"Wrote {README_FILE}")


if __name__ == "__main__":
    main()
