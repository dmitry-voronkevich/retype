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


def test_lockfile_has_a_windows_pyqt_runtime_wheel():
    project = (ROOT / "pyproject.toml").read_text()
    lockfile = (ROOT / "uv.lock").read_text()

    assert '"PyQt5-Qt5==5.15.2; sys_platform == \'win32\'"' in project
    start = lockfile.index('[[package]]\nname = "pyqt5-qt5"')
    windows_qt = lockfile[start:].split("[[package]]", 2)[1]

    assert 'version = "5.15.2"' in windows_qt
    assert "sys_platform == 'win32'" in windows_qt
    assert "PyQt5_Qt5-5.15.2-py3-none-win_amd64.whl" in windows_qt
