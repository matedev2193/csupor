"""Real image uploads, private serving, safe normalisation and durable avatars."""

import os
import struct
import tempfile
import unittest
import zlib
from io import BytesIO
from unittest.mock import Mock, patch

from flask import g
from flask_login import login_user
from PIL import Image, PngImagePlugin
from sqlalchemy import create_mock_engine, event, inspect, text
from sqlalchemy.dialects import mysql

from app import create_app, db
from app.models import ProfilePhoto, ProfilePhotoSource, User, UserPrivilege
from app.profile_photos import MAX_PHOTO_BYTES, profile_photo_url
from app.schema import create_missing_tables


def image_bytes(format="JPEG", size=(800, 400), colour=(20, 100, 180), **options):
    output = BytesIO()
    image = Image.new("RGB", size, colour)
    image.save(output, format=format, **options)
    return output.getvalue()


def png_dimensions(width, height):
    """A huge declared image for bounds checks without allocating its pixels."""
    data = bytearray(image_bytes("PNG", size=(1, 1)))
    data[16:24] = struct.pack(">II", width, height)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    return bytes(data)


class ProfilePhotoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_url = "sqlite:///" + os.path.join(self.directory.name, "photos.db")
        with patch.dict(os.environ, {"DATABASE_URL": self.database_url, "SECRET_KEY": "photo-test-only"}):
            self.app = create_app()
        self.app.config.update(TESTING=True)
        self.context = self.app.app_context()
        self.context.push()
        self.client = self.app.test_client()
        self.users = {}
        for name in ("employee", "other", "hr", "ceo", "developer"):
            privilege = UserPrivilege.employee if name == "other" else UserPrivilege(name)
            user = User(username=name, email=f"{name}@example.invalid", password_hash="unused", privilege=privilege)
            db.session.add(user)
            self.users[name] = user
        db.session.commit()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.directory.cleanup()

    def login(self, name):
        with self.client.session_transaction() as session:
            session["_user_id"] = self.users[name].get_id()
            session["_fresh"] = True
            session["locale"] = "en"
        g.pop("_login_user", None)

    def token(self):
        response = self.client.get("/profile")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            return session["profile_photo_csrf_token"]

    def upload(self, payload=None, filename="photo.jpg", csrf=None, **extra):
        data = {
            "csrf_token": self.token() if csrf is None else csrf,
            "photo": (BytesIO(image_bytes() if payload is None else payload), filename, "text/html"),
        }
        data.update(extra)
        response = self.client.post("/profile/photo", data=data)
        # Werkzeug's multipart test input can use a temporary file for large uploads.
        self.addCleanup(response.request.environ["wsgi.input"].close)
        return response

    def saved(self, name="employee"):
        db.session.expire_all()
        return db.session.get(ProfilePhoto, self.users[name].id)

    def test_all_supported_formats_decode_to_metadata_free_thumbnails(self):
        self.login("employee")
        for format, filename in (("JPEG", "camera.JPEG"), ("PNG", "camera.png"), ("WEBP", "camera.webp")):
            with self.subTest(format=format):
                uploaded = image_bytes(format)
                response = self.upload(uploaded, filename)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, "/profile")
                saved = self.saved()
                self.assertEqual((saved.width, saved.height), (384, 192))
                self.assertEqual(saved.mime_type, "image/jpeg")
                self.assertEqual(saved.size_bytes, len(saved.data))
                self.assertEqual(len(saved.version), 32)
                self.assertIsNotNone(saved.updated_at)
                with Image.open(BytesIO(saved.data)) as result:
                    result.load()
                    self.assertEqual(result.format, "JPEG")
                    self.assertEqual(result.mode, "RGB")
                    self.assertFalse(result.getexif())
                self.assertNotEqual(saved.data, uploaded)
        self.assertEqual(ProfilePhoto.query.count(), 1)

    def test_exif_orientation_is_applied_and_private_metadata_removed(self):
        self.login("employee")
        image = Image.new("RGB", (80, 40), "red")
        image.paste("blue", (40, 0, 80, 40))
        exif = Image.Exif()
        exif[274] = 6
        exif[270] = "Private description"
        exif[315] = "Private photographer"
        output = BytesIO()
        image.save(output, format="JPEG", exif=exif)
        self.upload(output.getvalue())
        saved = self.saved()
        self.assertNotIn(b"Private", saved.data)
        with Image.open(BytesIO(saved.data)) as result:
            self.assertEqual(result.size, (40, 80))
            self.assertFalse(result.getexif())
            self.assertGreater(result.getpixel((20, 10))[0], 220)
            self.assertGreater(result.getpixel((20, 70))[2], 220)

    def test_transparent_png_has_white_background_and_no_embedded_text(self):
        self.login("employee")
        image = Image.new("RGBA", (20, 20), (0, 0, 255, 0))
        pnginfo = PngImagePlugin.PngInfo()
        pnginfo.add_text("Comment", "Sensitive location")
        output = BytesIO()
        image.save(output, format="PNG", pnginfo=pnginfo)
        self.upload(output.getvalue(), "photo.png")
        saved = self.saved()
        self.assertNotIn(b"Sensitive", saved.data)
        with Image.open(BytesIO(saved.data)) as result:
            self.assertEqual(result.getpixel((10, 10)), (255, 255, 255))

    def test_image_is_visible_only_to_owner_hr_and_ceo(self):
        self.login("employee")
        self.upload()
        owner_id = self.users["employee"].id
        expected_bytes = self.saved().data
        for name in self.users:
            with self.subTest(viewer=name):
                self.login(name)
                response = self.client.get(f"/users/{owner_id}/photo")
                allowed = name in {"employee", "hr", "ceo"}
                self.assertEqual(response.status_code, 200 if allowed else 403)
                if allowed:
                    self.assertEqual(response.data, expected_bytes)
                    self.assertEqual(response.mimetype, "image/jpeg")
                    self.assertEqual(response.headers["Cache-Control"], "private, no-store")
                    self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
                    self.assertIn("inline", response.headers["Content-Disposition"])
        self.login("hr")
        self.assertEqual(self.client.get("/users/99999/photo").status_code, 404)
        self.client.get("/logout")
        g.pop("_login_user", None)
        self.assertEqual(self.client.get(f"/users/{owner_id}/photo").status_code, 302)
        self.assertEqual(self.upload(csrf="invalid").status_code, 302)

    def test_upload_cannot_change_another_users_photo(self):
        self.login("other")
        self.upload(image_bytes(colour=(200, 0, 0)))
        other_photo = self.saved("other").data
        self.login("employee")
        self.upload(user_id=str(self.users["other"].id))
        self.assertIsNotNone(self.saved())
        self.assertEqual(self.saved("other").data, other_photo)
        for name in ("hr", "ceo", "developer"):
            self.login(name)
            self.assertEqual(self.upload(user_id=str(self.users["other"].id)).status_code, 302)
            self.assertIsNotNone(self.saved(name))
            self.assertEqual(self.saved("other").data, other_photo)

    def test_replacement_changes_version_and_invalid_uploads_preserve_previous_photo(self):
        self.login("employee")
        self.upload()
        first_version = self.saved().version
        self.upload(image_bytes(colour=(180, 40, 20)))
        original_data, version = self.saved().data, self.saved().version
        self.assertNotEqual(first_version, version)
        invalid = [
            (b"", "empty.jpg"), (b"<svg onload='bad()'/>", "photo.svg"),
            (b"not an image", "photo.jpg"), (image_bytes("PNG"), "photo.jpg"),
            (image_bytes()[:200], "broken.jpg"), (image_bytes(), "photo.exe"),
            (image_bytes(), ""), (png_dimensions(5000, 5000), "huge.png"),
            (png_dimensions(10_001, 1), "wide.png"),
            (png_dimensions(50_000, 50_000), "bomb.png"),
        ]
        for payload, filename in invalid:
            with self.subTest(filename=filename):
                self.assertEqual(self.upload(payload, filename).status_code, 302)
                saved = self.saved()
                self.assertEqual((saved.data, saved.version), (original_data, version))
        self.assertEqual(ProfilePhoto.query.count(), 1)
        response = self.client.post("/profile/photo", data={"csrf_token": self.token()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.saved().data, original_data)

    def test_csrf_is_required_before_updates(self):
        self.login("employee")
        self.upload()
        original = self.saved().data
        for csrf in ("", "invalid", "árvíz"):
            self.assertEqual(self.upload(image_bytes(colour=(1, 2, 3)), csrf=csrf).status_code, 400)
            self.assertEqual(self.saved().data, original)

    def test_exact_five_megabytes_accepted_and_larger_uploads_rejected(self):
        self.login("employee")
        data = image_bytes()
        exact = data + b"\0" * (MAX_PHOTO_BYTES - len(data))
        self.assertEqual(self.upload(exact).status_code, 302)
        photo = self.saved()
        self.assertIsNotNone(photo)
        self.assertLess(photo.size_bytes, len(data))
        before = photo.data, photo.version
        for too_large in (exact + b"x", exact + b"x" * (128 * 1024)):
            self.assertEqual(self.upload(too_large).status_code, 302)
            self.assertEqual((self.saved().data, self.saved().version), before)

    def test_photo_urls_and_normal_profile_rendering_never_load_binary(self):
        self.login("employee")
        self.upload()
        db.session.expire_all()
        photo = db.session.get(ProfilePhoto, self.users["employee"].id)
        self.assertIn("data", inspect(photo).unloaded)
        source = db.session.get(ProfilePhotoSource, self.users["employee"].id)
        self.assertIn("data", inspect(source).unloaded)
        statements = []

        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", collect)
        try:
            with self.app.test_request_context("/profile"):
                login_user(self.users["employee"])
                url = profile_photo_url(self.users["employee"])
                self.assertIn(f"/users/{self.users['employee'].id}/photo?v=", url)
                self.assertIsNone(profile_photo_url(self.users["other"]))
            response = self.client.get("/profile")
            self.assertEqual(response.status_code, 200)
        finally:
            event.remove(db.engine, "before_cursor_execute", collect)
        self.assertFalse(any("profile_photos.data" in sql for sql in statements))
        self.assertFalse(any("profile_photo_sources.data" in sql for sql in statements))

    def test_unknown_length_multipart_is_bounded_before_file_decoding(self):
        self.login("employee")
        self.upload()
        before = self.saved().data, self.saved().version
        streams = []
        original_stream_factory = self.app.request_class._get_file_stream

        def track_stream(request, *args, **kwargs):
            stream = original_stream_factory(request, *args, **kwargs)
            streams.append(stream)
            return stream

        with patch.object(self.app.request_class, "_get_file_stream", track_stream):
            response = self.client.post(
                "/profile/photo",
                data={"csrf_token": self.token(), "photo": (BytesIO(image_bytes() + b"x" * (12 * 1024 * 1024)), "large.jpg")},
                environ_overrides={"CONTENT_LENGTH": "", "wsgi.input_terminated": True},
            )
        self.addCleanup(response.request.environ["wsgi.input"].close)
        self.assertEqual(response.status_code, 413)
        self.assertTrue(streams)
        self.assertTrue(all(stream.closed for stream in streams))
        self.assertEqual((self.saved().data, self.saved().version), before)

    def test_user_deletion_removes_photo_in_orm_and_database(self):
        self.login("employee")
        self.upload()
        employee_id = self.users["employee"].id
        db.session.delete(self.users["employee"])
        db.session.commit()
        self.assertIsNone(db.session.get(ProfilePhoto, employee_id))
        self.assertIsNone(db.session.get(ProfilePhotoSource, employee_id))
        self.login("other")
        self.upload()
        other_id = self.users["other"].id
        db.session.remove()
        with db.engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            self.assertEqual(connection.execute(text("PRAGMA foreign_keys")).scalar(), 1)
            connection.execute(User.__table__.delete().where(User.id == other_id))
            self.assertEqual(connection.execute(ProfilePhoto.__table__.select()).all(), [])
            self.assertEqual(connection.execute(ProfilePhotoSource.__table__.select()).all(), [])

    def test_photo_survives_application_restart(self):
        self.login("employee")
        self.upload()
        user_id = self.users["employee"].id
        expected = self.saved().data
        expected_source = db.session.get(ProfilePhotoSource, user_id).data
        with patch.dict(os.environ, {"DATABASE_URL": self.database_url, "SECRET_KEY": "photo-test-only"}):
            restarted = create_app()
        restarted.config.update(TESTING=True)
        with restarted.app_context():
            new_client = restarted.test_client()
            with new_client.session_transaction() as session:
                session["_user_id"] = db.session.get(User, user_id).get_id()
                session["_fresh"] = True
            response = new_client.get(f"/users/{user_id}/photo")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, expected)
            self.assertEqual(db.session.get(ProfilePhotoSource, user_id).data, expected_source)
            db.session.remove()
            db.engine.dispose()

    def test_mysql_photo_table_matches_existing_identifier_types(self):
        original_type = ProfilePhoto.__table__.c.user_id.type
        for id_type, expected in (
            (mysql.INTEGER(), "INTEGER"), (mysql.INTEGER(unsigned=True), "INTEGER UNSIGNED"),
            (mysql.BIGINT(unsigned=True), "BIGINT UNSIGNED"),
        ):
            with self.subTest(type=expected):
                statements = []
                engine = create_mock_engine("mysql+mysqlconnector://", lambda statement, *a, **kw: statements.append(str(statement.compile(dialect=mysql.dialect()))))
                inspector = Mock()
                inspector.get_table_names.return_value = [name for name in db.metadata.tables if name != "profile_photos"]
                inspector.get_columns.return_value = [{"name": "id", "type": id_type}]
                with patch("app.schema.inspect", return_value=inspector):
                    create_missing_tables(engine, db.metadata)
                self.assertEqual(len(statements), 1)
                self.assertIn("CREATE TABLE profile_photos", statements[0])
                self.assertIn(f"user_id {expected} NOT NULL", statements[0])
                self.assertIn("data MEDIUMBLOB NOT NULL", statements[0])
                self.assertIn("FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE", statements[0])
                self.assertIs(ProfilePhoto.__table__.c.user_id.type, original_type)


if __name__ == "__main__":
    unittest.main()
