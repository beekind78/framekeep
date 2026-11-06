import csv
import shutil
from pathlib import Path

import pytest
from PIL import Image

from framekeep.image_processing import process_image
from framekeep.library import Library, file_hash
from tests.media_fixtures import video_bytes


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path / "app-data")


def photo(path, color="green", date=None, orientation=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (80, 50), color)
    exif = Image.Exif()
    if date:
        exif[306] = date
    if orientation:
        exif[274] = orientation
    image.save(path, exif=exif)
    return path


def scan(library, root):
    library_id = library.add(str(root))
    return library.scan(library_id, lambda *args: None, lambda: False)


def test_date_sources_corrupt_files_duplicates_and_incremental_scan(library, tmp_path):
    source = tmp_path / "backup"
    first = photo(source / "original.jpg", date="2020:02:03 04:05:06")
    shutil.copy2(first, source / "copy.jpg")
    photo(source / "IMG-20211231-WA0001.png", color="red")
    photo(source / "no-date.jpg", color="blue")
    (source / "broken.jpg").write_bytes(b"not a photo")
    result = scan(library, source)
    assert result["indexed"] == 4
    assert result["error_count"] == 1
    data = library.summary()
    assert data["total"] == 4
    assert data["duplicates"]["extra_copies"] == 1
    rows = {p["filename"]: p for p in library.photos({}, None)["photos"]}
    assert rows["original.jpg"]["captured_at"] == "2020-02-03T04:05:06"
    assert rows["original.jpg"]["date_source"] == "Camera metadata"
    assert rows["IMG-20211231-WA0001.png"]["captured_at"].startswith("2021-12-31")
    assert rows["IMG-20211231-WA0001.png"]["date_source"] == "Filename"
    assert rows["no-date.jpg"]["date_source"] == "File modified"
    assert scan(library, source)["indexed"] == 0
    assert library.photos({"duplicates": True}, None)["total"] == 2
    assert library.photos({"year": "2021"}, None)["total"] == 1
    assert library.photos({"q": "%"}, None)["total"] == 0


def test_copy_is_verified_deduplicated_and_never_overwrites(library, tmp_path):
    source = tmp_path / "backup"
    first = photo(source / "one" / "same.jpg", date="2019:01:01 00:00:00")
    second = photo(source / "two" / "same.jpg", color="red", date="2019:01:01 00:00:00")
    shutil.copy2(first, source / "extra.jpg")
    original_hashes = {str(p): file_hash(p) for p in source.rglob("*.jpg")}
    scan(library, source)
    output = tmp_path / "output"
    output.mkdir()
    preview = library.plan(str(output), "date", {}, True)
    assert preview["count"] == 2
    assert preview["skipped"] == 1
    result = library.export(library.consume_plan(preview["token"]), lambda *args: None, lambda: False)
    assert result["copied"] == 2
    assert result["error_count"] == 0
    copies = list(Path(result["destination"]).rglob("*.jpg"))
    assert len(copies) == 2
    assert {file_hash(p) for p in copies} == {file_hash(first), file_hash(second)}
    assert original_hashes == {str(p): file_hash(p) for p in source.rglob("*.jpg")}
    assert len({p.name for p in copies}) == 2
    with (Path(result["destination"]) / "manifest.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert all(row["status"] == "verified" for row in csv.DictReader(stream))
    with pytest.raises(ValueError, match="expired"):
        library.consume_plan(preview["token"])


def test_changed_sources_are_not_copied(library, tmp_path):
    source = tmp_path / "backup"
    original = photo(source / "photo.jpg")
    scan(library, source)
    output = tmp_path / "output"
    output.mkdir()
    preview = library.plan(str(output), "type", {}, False)
    original.write_bytes(b"changed after indexing")
    result = library.export(library.consume_plan(preview["token"]), lambda *args: None, lambda: False)
    assert result["copied"] == 0
    assert result["error_count"] == 1
    assert not list(Path(result["destination"]).rglob("*.jpg"))


def test_source_overlap_and_missing_files(library, tmp_path):
    source = tmp_path / "backup"
    original = photo(source / "nested" / "photo.jpg")
    scan(library, source)
    with pytest.raises(ValueError, match="overlaps"):
        library.add(str(source / "nested"))
    with pytest.raises(ValueError, match="separate"):
        library.plan(str(source), "date", {}, False)
    original.unlink()
    scan(library, source)
    assert library.summary()["total"] == 0


def test_orientation_and_heic(library, tmp_path):
    source = tmp_path / "backup"
    photo(source / "rotated.jpg", orientation=6)
    photo(source / "iphone.heic", color="orange", date="2022:03:04 00:00:00")
    result = scan(library, source)
    assert result["error_count"] == 0
    rows = {p["filename"]: p for p in library.photos({}, None)["photos"]}
    with Image.open(library.thumbs / (str(rows["rotated.jpg"]["id"]) + ".jpg")) as image:
        assert image.height > image.width
    assert rows["iphone.heic"]["extension"] == "HEIC"


def test_mpo_uses_primary_still_and_keeps_all_frames_in_verified_copies(library, tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    original = source / "multi-image.JPG"
    primary = Image.new("RGB", (80, 60), "red")
    secondary = Image.new("RGB", (80, 60), "blue")
    primary.save(original, format="MPO", save_all=True, append_images=[secondary])
    contents = original.read_bytes()

    result = scan(library, source)
    assert result["found"] == result["extension_mismatches"] == 1
    assert result["error_count"] == 0
    item = library.photos({}, None)["photos"][0]
    assert item["extension"] == "MPO" and item["kind"] != "Animated"
    with Image.open(library.thumbs / f"{item['id']}.jpg") as thumbnail:
        red, _, blue = thumbnail.getpixel((40, 30))
        assert red > 200 and blue < 20

    output = tmp_path / "output"
    output.mkdir()
    preview = library.plan(str(output), "type", {}, False)
    export = library.export(library.consume_plan(preview["token"]), lambda *args: None, lambda: False)
    assert export["copied"] == 1
    copy = next(output.rglob("*.mpo"))
    assert copy.read_bytes() == original.read_bytes() == contents


def test_incomplete_jpeg_is_skipped_with_a_backup_hint(library, tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    original = source / "partial.jpg"
    Image.effect_noise((320, 240), 60).convert("RGB").save(original)
    partial = original.read_bytes()[: original.stat().st_size // 2]
    original.write_bytes(partial)
    result = scan(library, source)
    assert result["found"] == 0 and result["error_count"] == 1
    assert "incomplete (truncated)" in result["errors"][0]["reason"]
    assert original.read_bytes() == partial


def test_favorites_tags_selection_and_people_layout(library, tmp_path):
    source = tmp_path / "backup"
    photo(source / "family.jpg")
    scan(library, source)
    row = library.photos({}, None)["photos"][0]
    library.update_photo(row["id"], True, "family, holiday")
    assert library.photos({"favorite": True, "q": "holiday"}, None)["total"] == 1
    assert library.photos({"ids": [row["id"] + 1]}, None)["total"] == 0
    with library.connect() as db:
        person = db.execute("INSERT INTO persons(name) VALUES (?)", ("Alex/Family",)).lastrowid
        db.execute(
            "INSERT INTO faces(photo_id,person_id,embedding) VALUES (?,?,?)", (row["id"], person, "[]")
        )
    output = tmp_path / "output"
    output.mkdir()
    preview = library.plan(str(output), "people", {}, False)
    assert "Alex_Family" in preview["sample"][0]


def test_changed_unreadable_file_drops_stale_index(library, tmp_path):
    source = tmp_path / "backup"
    original = photo(source / "photo.jpg")
    scan(library, source)
    original.write_bytes(b"broken")
    assert scan(library, source)["error_count"] == 1
    assert library.summary()["total"] == 0


def test_original_exif_date_takes_priority_over_filename_and_file_date(library, tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    image = Image.new("RGB", (40, 30), "purple")
    exif = Image.Exif()
    exif[306] = "2025:06:01 12:00:00"
    exif[34665] = {36867: "2018:07:08 09:10:11"}
    image.save(source / "IMG_20201231_235959.jpg", exif=exif)
    scan(library, source)
    row = library.photos({}, None)["photos"][0]
    assert row["captured_at"] == "2018-07-08T09:10:11"
    assert row["date_source"] == "Camera metadata"


def test_mislabeled_iphone_backup_uses_content_and_preserves_originals(library, tmp_path):
    source = tmp_path / "backup"
    source.mkdir()
    exif = Image.Exif()
    exif[306] = "2021:06:07 08:09:10"
    for name, format_name in (("portrait.MOV", "JPEG"), ("memory.AAE", "HEIF"), ("screen.JPG", "PNG")):
        Image.new("RGB", (80, 50), "orange").save(source / name, format=format_name, exif=exif)
    (source / "video.HEIC").write_bytes(video_bytes())
    (source / "adjustments.PNG").write_bytes(b'<?xml version="1.0"?><plist><dict/></plist>')
    original_hashes = {p.name: file_hash(p) for p in source.iterdir()}

    library_id = library.add(str(source))
    result = library.scan(library_id, lambda *args: None, lambda: False)
    assert result["found"] == 3 and result["indexed"] == 4
    assert result["scanned_files"] == 5 and result["error_count"] == 0
    assert result["videos"] == 1
    assert result["skipped_media"] == {"videos": 0, "metadata": 1, "other": 0}
    assert result["extension_mismatches"] == 4
    rows = {p["filename"]: p for p in library.photos({"media_type": "image"}, None)["photos"]}
    assert {name: p["extension"] for name, p in rows.items()} == {
        "portrait.MOV": "JPG",
        "memory.AAE": "HEIC",
        "screen.JPG": "PNG",
    }
    assert all(p["captured_at"] == "2021-06-07T08:09:10" for p in rows.values())

    repeated = library.scan(library_id, lambda *args: None, lambda: False)
    assert repeated["indexed"] == 0 and repeated["found"] == 3
    assert repeated["extension_mismatches"] == 4
    output = tmp_path / "output"
    output.mkdir()
    preview = library.plan(str(output), "date", {"media_type": "image"}, False)
    copied = library.export(library.consume_plan(preview["token"]), lambda *args: None, lambda: False)
    assert copied["copied"] == 3 and copied["error_count"] == 0
    copies = list((Path(copied["destination"]) / "2021" / "06").iterdir())
    assert {p.name for p in copies} == {"portrait.jpg", "memory.heic", "screen.png"}
    assert {file_hash(p) for p in copies} == {original_hashes[name] for name in rows}
    assert original_hashes == {p.name: file_hash(p) for p in source.iterdir()}


@pytest.mark.parametrize(
    "container",
    [
        video_bytes(),
        video_bytes(legacy=True),
        video_bytes(brand=b"mp42"),
    ],
)
def test_video_with_photo_extension_is_indexed_without_image_decoding(library, tmp_path, container):
    source = tmp_path / "source"
    source.mkdir()
    path = source / "video.HEIC"
    path.write_bytes(container)
    result = scan(library, source)
    assert result["found"] == 0 and result["error_count"] == 0
    assert result["videos"] == 1
    assert library.summary()["video_total"] == 1
    with pytest.raises(ValueError, match="contains a video"):
        process_image(path, source, 480)


def test_revalidation_updates_detected_extension_and_explains_unreadable_photo(library, tmp_path):
    source = tmp_path / "backup"
    path = photo(source / "photo.jpg")
    library_id = library.add(str(source))
    library.scan(library_id, lambda *args: None, lambda: False)
    with library.connect() as db:
        db.execute("UPDATE photos SET extension='HEIC', policy_version=0")
    library.scan(library_id, lambda *args: None, lambda: False)
    assert library.photos({}, None)["photos"][0]["extension"] == "JPG"
    path.write_bytes(b"incomplete image")
    result = library.scan(library_id, lambda *args: None, lambda: False)
    assert result["error_count"] == 1
    assert "incomplete or unsupported" in result["errors"][0]["reason"]
    assert "BufferedReader" not in result["errors"][0]["reason"]
