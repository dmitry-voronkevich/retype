from retype.resource_handler import _root_from_runtime


def test_legacy_source_run_root_resolves_from_the_package_file():
    root = _root_from_runtime(
        False, False, '/ignored/python',
        '/workspace/retype/retype/resource_handler.py', '/ignored/argv')

    assert root == '/workspace/retype'


def test_legacy_packaged_app_root_resolves_beside_the_executable():
    root = _root_from_runtime(
        True, False,
        '/Applications/retype.app/Contents/MacOS/retype',
        '/ignored/resource_handler.py', '/ignored/argv')

    assert root == '/Applications/retype.app/Contents/MacOS'
