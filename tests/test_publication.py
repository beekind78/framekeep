import subprocess
from pathlib import Path

import pytest

from scripts.check_publication import PUBLIC_ASSETS, check


@pytest.fixture
def source_snapshot(tmp_path, monkeypatch):
    project = Path(__file__).resolve().parents[1]
    for path in PUBLIC_ASSETS:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((project / path).read_bytes())
    (tmp_path / "README.md").write_text("A sample source release.\n")
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "add", "--all"], check=True)
    return tmp_path


def test_reviewed_source_snapshot_passes(source_snapshot):
    result = check(staged=True)
    assert result["files"] == 4
    assert set(result["reviewed_binary_assets"]) == set(PUBLIC_ASSETS)


@pytest.mark.parametrize("name", [
    "IMG_4321.HEIC", "export.csv", "data/library.sqlite3", "runtime/instance.bin", ".env", ".local-data/note.txt",
])
def test_private_media_catalogs_credentials_and_diagnostics_are_rejected(source_snapshot, name):
    path = source_snapshot / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"private test placeholder")
    subprocess.run(["git", "add", "--", name], check=True)
    with pytest.raises(ValueError, match="unapproved path"):
        check(staged=True)


def test_replacing_reviewed_screenshot_requires_a_new_review(source_snapshot):
    path = "docs/screenshots/gallery.jpg"
    (source_snapshot / path).write_bytes(b"different test image")
    subprocess.run(["git", "add", "--", path], check=True)
    with pytest.raises(ValueError, match="differs from the reviewed demo asset"):
        check(staged=True)


def test_hardcoded_credential_is_rejected_without_printing_it(source_snapshot):
    secret = "gh" + "p_" + "A" * 36
    (source_snapshot / "README.md").write_text("credential=" + secret)
    subprocess.run(["git", "add", "README.md"], check=True)
    with pytest.raises(ValueError, match="possible credential") as error:
        check(staged=True)
    assert secret not in str(error.value)


def test_personal_profile_path_is_rejected(source_snapshot):
    private_path = "C:/" + "Users/" + "personal-user/Pictures"
    (source_snapshot / "README.md").write_text(private_path)
    subprocess.run(["git", "add", "README.md"], check=True)
    with pytest.raises(ValueError, match="non-example user profile"):
        check(staged=True)
