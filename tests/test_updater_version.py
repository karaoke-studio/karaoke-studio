"""KS 四段版本比较契约测试；不得替换为 SUG 三段解析器。"""

import pytest

from krok_helper.updater.worker import _strip_tag_prefix, _version_key, is_newer_version


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3.1.7", (3, 1, 7)),
        ("v3.1.7.4", (3, 1, 7, 4)),
        ("V3.2.0", (3, 2, 0)),
        ("3.2.0-beta1", (3, 2, 0, 0)),
        ("3.2", (3, 2, 0)),
    ],
)
def test_version_key(value, expected):
    assert _version_key(value) == expected


def test_fourth_segment_is_newer_not_a_prerelease():
    assert is_newer_version("3.1.7.4", "3.1.7") is True
    assert is_newer_version("3.1.7", "3.1.7.4") is False
    assert is_newer_version("3.2.0", "3.1.7.4") is True


def test_equal_and_older_versions_are_not_updates():
    assert is_newer_version("v3.1.7.4", "3.1.7.4") is False
    assert is_newer_version("3.1.7.3", "3.1.7.4") is False


def test_only_v_prefix_is_stripped():
    assert _strip_tag_prefix("v3.2.0") == "3.2.0"
    assert _strip_tag_prefix("SUGv1.3.7") == "SUGv1.3.7"
