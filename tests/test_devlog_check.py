import importlib.util

from petsourcing import config

spec = importlib.util.spec_from_file_location(
    "chk", config.REPO_ROOT / "scripts" / "check_devlog_append_only.py"
)
chk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chk)


def test_append_only():
    assert chk.is_append_only("a\n", "a\nb\n")
    assert chk.is_append_only("a\n", "a\n")
    assert not chk.is_append_only("a\nb\n", "a\n")
    assert not chk.is_append_only("a\n", "A\nb\n")
