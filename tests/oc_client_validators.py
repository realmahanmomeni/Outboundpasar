"""Optional real-parser validation of generated subscription bodies.

Binaries are never bundled.  Point these environment variables at official release builds to turn
the parser-level checks on; without them the helpers report ``available() is False`` and callers
must skip (a skipped check is NOT a pass):

* ``SINGBOX_BIN``  - SagerNet sing-box (``sing-box check -c``)
* ``XRAY_BIN``     - XTLS Xray-core (``xray run -test -c``)
* ``MIHOMO_BIN``   - MetaCubeX mihomo (``mihomo -t -f``; classic Clash is checked with the same core)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    output: str


def _binary(env: str, *names: str) -> str | None:
    value = os.environ.get(env, "").strip()
    if value and Path(value).is_file():
        return value
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def singbox_bin() -> str | None:
    return _binary("SINGBOX_BIN", "sing-box")


def xray_bin() -> str | None:
    return _binary("XRAY_BIN", "xray")


def mihomo_bin() -> str | None:
    return _binary("MIHOMO_BIN", "mihomo")


def _run(cmd: list[str], cwd: str, extra_env: dict[str, str] | None = None) -> ValidationResult:
    env = {**os.environ, **(extra_env or {})}
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60, check=False, env=env)
    return ValidationResult(proc.returncode == 0, (proc.stdout + proc.stderr).strip())


def _scrub(text: str) -> str:
    """Keep one diagnostic line (parsers echo banners and may echo config fragments)."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        low = line.lower()
        if any(marker in low for marker in ("failed to start", "fatal", "level=error", "test failed", "unknown field")):
            return line[:400]
    return (lines[-1] if lines else "")[:400]


def check_singbox(config_json: str) -> ValidationResult:
    binary = singbox_bin()
    assert binary, "sing-box binary not available"
    # The default template carries Android-only route options that the Linux core rejects; they are
    # unrelated to outbound schema, so they are stripped. Everything else is validated unchanged.
    doc = json.loads(config_json)
    route = doc.get("route")
    if isinstance(route, dict):
        for key in ("override_android_vpn", "find_process"):
            route.pop(key, None)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "config.json")
        path.write_text(json.dumps(doc))
        # sing-box >= 1.12 treats a missing ``domain_resolver`` as a deprecation error. That is a
        # property of the shipped template (not of OC entries) and is tracked separately; opt back
        # in so this check reports outbound *schema* errors only.
        result = _run(
            [binary, "check", "-c", str(path)], tmp, {"ENABLE_DEPRECATED_MISSING_DOMAIN_RESOLVER": "true"}
        )
    return ValidationResult(result.ok, _scrub(result.output))


def check_xray(config_json: str) -> list[ValidationResult]:
    """Validate every entry of an Xray subscription (a list of full configs) independently."""
    binary = xray_bin()
    assert binary, "xray binary not available"
    entries = json.loads(config_json)
    results: list[ValidationResult] = []
    with tempfile.TemporaryDirectory() as tmp:
        for index, entry in enumerate(entries):
            path = Path(tmp, f"entry-{index}.json")
            path.write_text(json.dumps(entry))
            result = _run([binary, "run", "-test", "-c", str(path)], tmp)
            results.append(ValidationResult(result.ok, _scrub(result.output)))
    return results


def check_mihomo(config_yaml: str) -> ValidationResult:
    binary = mihomo_bin()
    assert binary, "mihomo binary not available"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp, "config.yaml")
        path.write_text(config_yaml)
        result = _run([binary, "-t", "-d", tmp, "-f", str(path)], tmp)
    return ValidationResult(result.ok, _scrub(result.output))
