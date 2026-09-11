from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_ci_uses_supported_python_and_locked_test_group():
    workflow = (ROOT / ".github/workflows/run-tests.yml").read_text()

    assert "PYTHON_VERSION: '3.11'" in workflow
    assert "actions/setup-python@v5" in workflow
    assert "astral-sh/setup-uv@v5" in workflow
    assert "uv sync --locked --group test" in workflow
    assert "uv run --locked --group test pytest" in workflow
    assert "3.7.9" not in workflow
