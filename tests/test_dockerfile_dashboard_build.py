"""Guards Docker image build integration for the dashboard frontend."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dockerfile_builds_dashboard_in_multi_stage_pipeline() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    assert "FROM oven/bun:" in dockerfile
    assert "AS dashboard-builder" in dockerfile
    assert "bun install --frozen-lockfile" in dockerfile
    assert "bun run build" in dockerfile
    assert "COPY --from=dashboard-builder" in dockerfile
    assert "/code/dashboard/build" in dockerfile

    assert "dashboard_src\n" not in dockerignore and "dashboard_src\r\n" not in dockerignore
    assert "!dashboard/build" not in dockerignore


def test_build_dashboard_script_matches_docker_env() -> None:
    script = (REPO_ROOT / "build_dashboard.sh").read_text(encoding="utf-8")
    assert 'export VITE_BASE_API=/' in script
    assert 'cp "${OUT}/index.html" "${OUT}/404.html"' in script


def test_dashboard_spinner_fix_source_marker() -> None:
    """Tabbed-route suspense uses compact ContentSpinner (min-h-48), not full-screen-only loading."""
    source = (
        REPO_ROOT / "dashboard_src/src/components/layout/tabbed-route-suspense-fallback.tsx"
    ).read_text(encoding="utf-8")
    assert "function ContentSpinner()" in source
    assert "min-h-48" in source
    assert "TabbedRouteSuspenseFallback" in source
