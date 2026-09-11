from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_macos_bundle_spec_defines_a_finder_launchable_gui_app():
    spec = (ROOT / "setup/retype-target-bundle.spec").read_text()

    assert "console=False" in spec
    assert '"CFBundleExecutable": data.name' in spec
    assert 'bundle_identifier="io.github.plu5.retype"' in spec
    assert "icon=data.icns" in spec
    assert "../style/icons/retype.icns" in (
        ROOT / "setup/config/data.py").read_text()
    assert "elif ismacos:\n    # The legacy macOS exclusions" in (
        ROOT / "setup/config/binaries.py").read_text()


def test_macos_dmg_script_uses_locked_dependencies_and_native_tools():
    script_path = ROOT / "scripts/build-macos-dmg.sh"
    script = script_path.read_text()

    assert script_path.stat().st_mode & 0o111
    assert "uv run --locked --group build python -m PyInstaller" in script
    assert "hdiutil create" in script
    assert "ln -s /Applications" in script
    assert "retype-dmg.icns" in script
    assert "appdmg" not in script


def test_release_workflow_uploads_the_dmg_created_by_the_script():
    workflow = (ROOT / ".github/workflows/main.yml").read_text()

    assert "astral-sh/setup-uv@v5" in workflow
    assert "uv sync --locked --group build" in workflow
    assert "./scripts/build-macos-dmg.sh" in workflow
    assert "artifact=./dist/retype.dmg" in workflow
    assert "appdmg" not in workflow
