"""Real crop geometry, editable sources, optimistic saves and private metadata."""

import os
import unittest
from datetime import datetime
from io import BytesIO
from unittest.mock import Mock, patch

from PIL import Image
from sqlalchemy import create_mock_engine, event, update
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError

from app import create_app, db
from app.models import ProfilePhoto, ProfilePhotoSource
from app.profile_photos import _avatar
from app.schema import create_missing_tables
from tests import test_profile_photos as photo_fixtures


image_bytes = photo_fixtures.image_bytes


def striped_image(size=(800, 400)):
    image = Image.new("RGB", size)
    colours = ("red", "lime", "blue", "yellow")
    for index, colour in enumerate(colours):
        image.paste(colour, (round(index * size[0] / 4), 0, round((index + 1) * size[0] / 4), size[1]))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class ProfilePhotoEditorTests(unittest.TestCase):
    # Reuse the account/database fixture without running the old tests twice.
    setUp = photo_fixtures.ProfilePhotoTests.setUp
    tearDown = photo_fixtures.ProfilePhotoTests.tearDown
    login = photo_fixtures.ProfilePhotoTests.login
    token = photo_fixtures.ProfilePhotoTests.token
    upload = photo_fixtures.ProfilePhotoTests.upload
    saved = photo_fixtures.ProfilePhotoTests.saved

    def post_crop(self, payload=None, *, recipe=(0.5, 0.5, 1), version="", existing=False, filename="photo.png", **extra):
        data = {"csrf_token": self.token()}
        if version is not None:
            data["photo_version"] = version
        if recipe is not None:
            data.update(zip(("crop_center_x", "crop_center_y", "crop_zoom"), map(str, recipe)))
        if existing:
            data["use_existing"] = "1"
        if payload is not None:
            data["photo"] = (BytesIO(payload), filename)
        data.update(extra)
        response = self.client.post("/profile/photo", data=data, headers={"Accept": "application/json"})
        self.addCleanup(response.request.environ["wsgi.input"].close)
        return response

    def snapshot(self):
        photo = self.saved()
        source = db.session.get(ProfilePhotoSource, self.users["employee"].id)
        return (
            photo.data, photo.version, photo.width, photo.height,
            source.data, source.width, source.height, source.center_x, source.center_y, source.zoom,
        )

    def assert_colour(self, data, expected):
        with Image.open(BytesIO(data)) as image:
            colour = image.getpixel((image.width // 2, image.height // 2))
            self.assertTrue(all(abs(a - b) < 12 for a, b in zip(colour, expected)), (colour, expected))

    def test_square_crop_uses_drag_coordinates_and_reedit_uses_full_source(self):
        self.login("employee")
        first = self.post_crop(striped_image(), recipe=(0.125, 0.5, 2))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers["Cache-Control"], "private, no-store")
        result = first.get_json()
        self.assertEqual(set(result), {"photo_url", "version", "editor"})
        self.assertEqual((self.saved().width, self.saved().height), (384, 384))
        self.assert_colour(self.saved().data, (255, 0, 0))
        source = db.session.get(ProfilePhotoSource, self.users["employee"].id)
        source_bytes = source.data
        self.assertEqual((source.width, source.height), (800, 400))
        self.assertEqual((result["editor"]["center_x"], result["editor"]["zoom"]), (0.125, 2))
        self.assertEqual(self.client.get(result["photo_url"]).data, self.saved().data)

        second = self.post_crop(recipe=(0.875, 0.5, 2), version=result["version"], existing=True)
        self.assertEqual(second.status_code, 200)
        self.assertNotEqual(result["version"], second.json["version"])
        self.assert_colour(self.saved().data, (255, 255, 0))
        self.assertEqual(db.session.get(ProfilePhotoSource, self.users["employee"].id).data, source_bytes)
        self.assertEqual(self.client.get("/profile/photo/editor").json, second.json["editor"])

    def test_zoom_one_and_eight_select_correct_source_area(self):
        self.login("employee")
        first = self.post_crop(striped_image(), recipe=(0.5, 0.5, 1))
        self.assertEqual(first.status_code, 200)
        with Image.open(BytesIO(self.saved().data)) as image:
            left, right = image.getpixel((50, 192)), image.getpixel((334, 192))
            self.assertGreater(left[1], 240)
            self.assertGreater(right[2], 240)
        second = self.post_crop(recipe=(0.375, 0.5, 8), version=first.json["version"], existing=True)
        self.assertEqual(second.status_code, 200)
        self.assert_colour(self.saved().data, (0, 255, 0))

    def test_exif_orientation_applies_to_source_and_crop_before_coordinates(self):
        self.login("employee")
        image = Image.new("RGB", (80, 40), "red")
        image.paste("blue", (40, 0, 80, 40))
        exif = Image.Exif()
        exif[274], exif[270] = 6, "Private location"
        output = BytesIO()
        image.save(output, format="JPEG", exif=exif)
        first = self.post_crop(output.getvalue(), filename="photo.jpg", recipe=(0.5, 0.25, 1))
        self.assertEqual(first.status_code, 200)
        self.assert_colour(self.saved().data, (255, 0, 0))
        self.assertEqual((first.json["editor"]["width"], first.json["editor"]["height"]), (40, 80))
        source = db.session.get(ProfilePhotoSource, self.users["employee"].id)
        self.assertNotIn(b"Private", source.data)
        with Image.open(BytesIO(source.data)) as image:
            self.assertFalse(image.getexif())
        second = self.post_crop(recipe=(0.5, 0.75, 1), version=first.json["version"], existing=True)
        self.assertEqual(second.status_code, 200)
        self.assert_colour(self.saved().data, (0, 0, 255))

    def test_large_source_is_bounded_and_exact_edge_rounding_can_be_edited_again(self):
        self.login("employee")
        version = ""
        for width, height in ((4095, 2001), (2001, 4095)):
            with self.subTest(size=(width, height)):
                side = min(width, height)
                recipe = (side / (2 * width), side / (2 * height), 1)
                result = self.post_crop(image_bytes("PNG", (width, height)), recipe=recipe, version=version)
                self.assertEqual(result.status_code, 200)
                metadata = result.json["editor"]
                self.assertEqual(max(metadata["width"], metadata["height"]), 2048)
                crop_side = min(metadata["width"], metadata["height"])
                self.assertGreaterEqual(metadata["center_x"], crop_side / (2 * metadata["width"]))
                self.assertGreaterEqual(metadata["center_y"], crop_side / (2 * metadata["height"]))
                version = result.json["version"]
                repeated = self.post_crop(
                    recipe=(metadata["center_x"], metadata["center_y"], metadata["zoom"]),
                    version=version, existing=True,
                )
                self.assertEqual(repeated.status_code, 200)
                version = repeated.json["version"]

    def test_invalid_crop_and_partial_or_nonfinite_fields_preserve_both_images(self):
        self.login("employee")
        result = self.post_crop(striped_image())
        version = result.json["version"]
        before = self.snapshot()
        invalid_recipes = [
            (-0.1, 0.5, 1), (1.1, 0.5, 1), (0.1, 0.5, 1), (0.5, 0.1, 1),
            (0.5, 0.5, 0.999), (0.5, 0.5, 8.001), (0.5, 0.5, "nan"),
            ("Infinity", 0.5, 1), (0.5, "-inf", 1), (0.5, 0.5, "1e999"),
            ("", 0.5, 1), (0.5, "bad", 1),
        ]
        for recipe in invalid_recipes:
            with self.subTest(recipe=recipe):
                response = self.post_crop(recipe=recipe, version=version, existing=True)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.json)
                self.assertEqual(self.snapshot(), before)
        for extras in ({}, {"crop_center_x": "0.5"}, {"crop_center_x": "0.5", "crop_zoom": "1"}):
            response = self.post_crop(recipe=None, existing=True, version=version, **extras)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.snapshot(), before)
        for payload, name in ((b"bad", "photo.png"), (image_bytes(), "mismatch.png")):
            response = self.post_crop(payload, filename=name, version=version)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(self.snapshot(), before)

    def test_missing_and_stale_versions_cannot_overwrite_images(self):
        self.login("employee")
        first = self.post_crop(striped_image())
        old_version = first.json["version"]
        second = self.post_crop(recipe=(0.875, 0.5, 2), version=old_version, existing=True)
        self.assertEqual(second.status_code, 200)
        before = self.snapshot()
        for arguments, expected in [
            ({"existing": True, "version": None}, 400),
            ({"existing": True, "version": old_version}, 409),
            ({"payload": striped_image(), "version": old_version}, 409),
            ({"payload": striped_image(), "version": ""}, 409),
        ]:
            response = self.post_crop(**arguments)
            self.assertEqual(response.status_code, expected)
            self.assertEqual(self.snapshot(), before)

    def test_version_is_rechecked_after_image_processing(self):
        self.login("employee")
        first = self.post_crop(striped_image())
        before = self.snapshot()
        winner_version = "f" * 32

        def intervening_save(image, recipe):
            result = _avatar(image, recipe)
            db.session.execute(update(ProfilePhoto).where(ProfilePhoto.user_id == self.users["employee"].id).values(version=winner_version))
            db.session.commit()
            return result

        with patch("app.profile_photos._avatar", side_effect=intervening_save):
            response = self.post_crop(recipe=(0.875, 0.5, 2), version=first.json["version"], existing=True)
        self.assertEqual(response.status_code, 409)
        after = self.snapshot()
        self.assertEqual(after[1], winner_version)
        self.assertEqual(after[:1] + after[2:], before[:1] + before[2:])

    def test_source_update_failure_rolls_back_avatar_update_as_well(self):
        self.login("employee")
        first = self.post_crop(striped_image())
        before = self.snapshot()
        db.session.execute(db.text("""
            CREATE TRIGGER reject_photo_source_update BEFORE UPDATE ON profile_photo_sources
            BEGIN SELECT RAISE(ABORT, 'test storage failure'); END
        """))
        db.session.commit()
        with self.assertRaises(IntegrityError):
            self.post_crop(recipe=(0.875, 0.5, 2), version=first.json["version"], existing=True)
        self.assertEqual(self.snapshot(), before)

    def test_legacy_avatar_becomes_editable_without_fabricating_an_original(self):
        self.login("employee")
        legacy_bytes = image_bytes("JPEG", (384, 192))
        legacy = ProfilePhoto(
            user_id=self.users["employee"].id, data=legacy_bytes, width=384, height=192,
            mime_type="image/jpeg", size_bytes=len(legacy_bytes), version="a" * 32, updated_at=datetime.now(),
        )
        db.session.add(legacy)
        db.session.commit()
        metadata = self.client.get("/profile/photo/editor").json
        self.assertEqual((metadata["width"], metadata["height"], metadata["zoom"]), (384, 192, 1))
        self.assertEqual(self.client.get(metadata["image_url"]).data, legacy_bytes)
        self.assertIsNone(db.session.get(ProfilePhotoSource, legacy.user_id))
        invalid = self.post_crop(recipe=(0, 0, 1), version=legacy.version, existing=True)
        self.assertEqual(invalid.status_code, 400)
        self.assertIsNone(db.session.get(ProfilePhotoSource, legacy.user_id))
        result = self.post_crop(version=legacy.version, existing=True)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(db.session.get(ProfilePhotoSource, legacy.user_id).data, legacy_bytes)

    def test_original_source_and_metadata_are_owner_only_and_private(self):
        self.login("employee")
        first = self.post_crop(striped_image())
        owner_id = self.users["employee"].id
        source_response = self.client.get(first.json["editor"]["image_url"])
        self.assertEqual(source_response.status_code, 200)
        self.assertEqual(source_response.mimetype, "image/jpeg")
        self.assertEqual(source_response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(source_response.headers["X-Content-Type-Options"], "nosniff")
        before = self.snapshot()
        for name in ("other", "hr", "ceo", "developer"):
            self.login(name)
            self.assertEqual(self.client.get(f"/profile/photo/source?user_id={owner_id}").status_code, 404)
            self.assertEqual(self.client.get(f"/profile/photo/editor?user_id={owner_id}").status_code, 404)
            response = self.post_crop(existing=True, version="", user_id=str(owner_id))
            self.assertEqual(response.status_code, 400)
        self.login("employee")
        self.assertEqual(self.snapshot(), before)
        self.client.get("/logout")
        from flask import g
        g.pop("_login_user", None)
        self.assertEqual(self.client.get("/profile/photo/source").status_code, 302)
        self.assertEqual(self.client.get("/profile/photo/editor").status_code, 302)

    def test_old_source_version_is_rejected_after_save(self):
        self.login("employee")
        first = self.post_crop(striped_image())
        second = self.post_crop(recipe=(0.875, 0.5, 2), version=first.json["version"], existing=True)
        self.assertEqual(self.client.get(first.json["editor"]["image_url"]).status_code, 409)
        self.assertEqual(self.client.get(second.json["editor"]["image_url"]).status_code, 200)

    def test_editor_metadata_does_not_fetch_source_or_avatar_bytes(self):
        self.login("employee")
        self.post_crop(striped_image())
        db.session.expire_all()
        statements = []

        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", collect)
        try:
            response = self.client.get("/profile/photo/editor")
        finally:
            event.remove(db.engine, "before_cursor_execute", collect)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("profile_photo_sources" in sql for sql in statements))
        self.assertFalse(any("profile_photo_sources.data" in sql or "profile_photos.data" in sql for sql in statements))

    def test_crop_recipe_and_source_survive_restart(self):
        self.login("employee")
        result = self.post_crop(striped_image(), recipe=(0.125, 0.5, 2))
        before = self.snapshot()
        user_id = self.users["employee"].id
        with patch.dict(os.environ, {"DATABASE_URL": self.database_url, "SECRET_KEY": "photo-test-only"}):
            restarted = create_app()
        with restarted.app_context():
            client = restarted.test_client()
            with client.session_transaction() as session:
                session["_user_id"] = str(user_id)
                session["_fresh"] = True
            self.assertEqual(client.get("/profile/photo/editor").json, result.json["editor"])
            self.assertEqual(client.get(result.json["editor"]["image_url"]).data, before[4])
            db.session.remove()
            db.engine.dispose()

    def test_source_table_matches_signed_unsigned_and_bigint_mysql_users(self):
        original_type = ProfilePhotoSource.__table__.c.user_id.type
        for id_type, expected in (
            (mysql.INTEGER(), "INTEGER"), (mysql.INTEGER(unsigned=True), "INTEGER UNSIGNED"),
            (mysql.BIGINT(unsigned=True), "BIGINT UNSIGNED"),
        ):
            with self.subTest(type=expected):
                statements = []
                engine = create_mock_engine("mysql+mysqlconnector://", lambda statement, *a, **kw: statements.append(str(statement.compile(dialect=mysql.dialect()))))
                inspector = Mock()
                inspector.get_table_names.return_value = [name for name in db.metadata.tables if name != "profile_photo_sources"]
                inspector.get_columns.return_value = [{"name": "id", "type": id_type}]
                with patch("app.schema.inspect", return_value=inspector):
                    create_missing_tables(engine, db.metadata)
                self.assertEqual(len(statements), 1)
                self.assertIn(f"user_id {expected} NOT NULL", statements[0])
                self.assertIn("data MEDIUMBLOB NOT NULL", statements[0])
                self.assertIn("center_x DOUBLE", statements[0])
                self.assertIn("FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE", statements[0])
                self.assertIs(ProfilePhotoSource.__table__.c.user_id.type, original_type)


if __name__ == "__main__":
    unittest.main()
