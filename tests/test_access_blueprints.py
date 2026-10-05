"""Page grants and blueprint handlers agree for uploads, settings and exports."""

import unittest
from io import BytesIO

import test_profile_photos as photo_fixtures
import test_worktime_routes as fixtures
from app import db
from app.mail_settings import get_mail_settings
from app.mail_settings_models import MailServerSettings
from app.models import ProfilePhoto
from app.page_access import invalidate_access_cache
from app.page_access_models import PageRolePermission
from app.worktime_models import WorkGroup
from app.worktime_service import settings_revision


class PageAccessBlueprintTests(unittest.TestCase):
    setUp = fixtures.WorktimeRoutesTests.setUp
    tearDown = fixtures.WorktimeRoutesTests.tearDown
    login = fixtures.WorktimeRoutesTests.login
    schedule = fixtures.WorktimeRoutesTests.schedule
    post = fixtures.WorktimeRoutesTests.post
    generate = fixtures.WorktimeRoutesTests.generate

    def rule(self, key, role, allowed):
        row = db.session.get(PageRolePermission, (key, role))
        if row is None:
            row = PageRolePermission(page_key=key, role=role)
            db.session.add(row)
        row.allowed = allowed
        db.session.commit()
        invalidate_access_cache()

    def upload_photo(self, colour):
        self.assertEqual(self.client.get("/profile").status_code, 200)
        with self.client.session_transaction() as session:
            token = session["profile_photo_csrf_token"]
        response = self.client.post("/profile/photo", data={
            "csrf_token": token,
            "photo": (BytesIO(photo_fixtures.image_bytes(size=(32, 32), colour=colour)), "photo.jpg"),
        })
        self.assertEqual(response.status_code, 302)

    def test_employee_group_grant_allows_real_group_creation_without_schedule_management(self):
        self.rule("worktime.groups", "employee", True)
        self.login("teacher1")
        for path in (f"/groups?place_id={self.site.id}", f"/groups/new?place_id={self.site.id}"):
            self.assertEqual(self.client.get(path).status_code, 200)
        response = self.client.post("/groups/new", data={
            "csrf_token": "test-worktime-csrf", "place_id": self.site.id,
            "settings_revision": settings_revision(self.site.id), "action": "group",
            "name": "Employee-managed group", "start_date": "2026-01-01",
        }, headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 200)
        created = WorkGroup.query.filter_by(name="Employee-managed group").one()
        self.assertEqual(self.client.get(f"/groups/{created.id}/edit").status_code, 200)
        self.assertEqual(self.client.get("/worktime/manage").status_code, 403)
        self.assertEqual(self.post("/worktime/generate").status_code, 403)
        self.assertIsNone(self.schedule())

    def test_revoked_hr_group_page_blocks_new_editor_and_legacy_form_before_write(self):
        self.rule("worktime.groups", "hr", False)
        original_name = self.groups[0].name
        original_count = WorkGroup.query.count()
        for path in ("/groups", f"/groups/new?place_id={self.site.id}",
                     f"/groups/{self.groups[0].id}/edit", "/worktime/groups"):
            self.assertEqual(self.client.get(path).status_code, 403)
        for path in ("/groups/new", f"/groups/{self.groups[0].id}/edit", "/worktime/groups"):
            self.assertEqual(self.post(path, action="group", group_id=self.groups[0].id,
                                       name="Forbidden change", start_date="2025-01-01",
                                       settings_revision=settings_revision(self.site.id)).status_code, 403)
        db.session.refresh(self.groups[0])
        self.assertEqual(self.groups[0].name, original_name)
        self.assertEqual(WorkGroup.query.count(), original_count)
        self.assertEqual(self.client.get("/worktime/manage?year=2026&month=2").status_code, 200)

    def test_mail_page_grant_allows_settings_handler_and_revocation_prevents_changes(self):
        self.rule("mail_settings.settings", "employee", True)
        self.login("teacher1")
        self.assertEqual(self.client.get("/settings").status_code, 200)
        with self.client.session_transaction() as session:
            token = session["mail_settings_csrf_token"]
        data = {
            "csrf_token": token, "revision": get_mail_settings().revision,
            "host": "smtp.example.invalid", "port": "587", "security": "starttls",
            "sender_email": "csupor@example.invalid", "sender_name": "CSUPOR",
            "base_url": "https://csupor.example.invalid",
        }
        # Store disabled settings only: this test never contacts an SMTP server.
        self.assertEqual(self.client.post("/settings", data=data).status_code, 302)
        saved = db.session.get(MailServerSettings, 1)
        self.assertEqual(saved.updated_by_id, self.users["teacher1"].id)
        self.assertFalse(saved.enabled)
        self.rule("mail_settings.settings", "employee", False)
        data.update(revision=saved.revision, host="forbidden.example.invalid")
        self.assertEqual(self.client.get("/settings").status_code, 403)
        self.assertEqual(self.client.post("/settings", data=data).status_code, 403)
        self.assertEqual(self.client.post("/settings/email-key", data={"csrf_token": token}).status_code, 403)
        db.session.refresh(saved)
        self.assertEqual(saved.host, "smtp.example.invalid")

    def test_profile_revocation_preserves_own_avatar_but_blocks_editing_and_source(self):
        self.login("teacher1")
        self.upload_photo((30, 120, 190))
        owner_id = self.users["teacher1"].id
        original = db.session.get(ProfilePhoto, owner_id).data
        self.assertEqual(self.client.get("/profile/photo/editor").status_code, 200)
        self.assertEqual(self.client.get("/profile/photo/source").status_code, 200)
        self.rule("edit_profile", "employee", False)
        own_avatar = self.client.get(f"/users/{owner_id}/photo")
        self.assertEqual(own_avatar.status_code, 200)
        self.assertEqual(own_avatar.data, original)
        self.assertEqual(self.client.get("/profile/photo/editor").status_code, 403)
        self.assertEqual(self.client.get("/profile/photo/source").status_code, 403)
        self.assertEqual(self.client.post("/profile/photo", data={"action": "delete"}).status_code, 403)
        self.assertEqual(db.session.get(ProfilePhoto, owner_id).data, original)

    def test_other_avatar_access_follows_profile_management_grant_in_handler(self):
        self.login("teacher1")
        self.upload_photo((80, 170, 20))
        owner_id = self.users["teacher1"].id
        expected = db.session.get(ProfilePhoto, owner_id).data
        self.login("teacher2")
        self.assertEqual(self.client.get(f"/users/{owner_id}/photo").status_code, 403)
        self.rule("manage_user_profiles", "employee", True)
        granted = self.client.get(f"/users/{owner_id}/photo")
        self.assertEqual(granted.status_code, 200)
        self.assertEqual(granted.data, expected)
        self.rule("manage_user_profiles", "employee", False)
        self.assertEqual(self.client.get(f"/users/{owner_id}/photo").status_code, 403)
        self.login("hr")
        self.assertEqual(self.client.get(f"/users/{owner_id}/photo").status_code, 200)
        self.rule("manage_user_profiles", "hr", False)
        self.assertEqual(self.client.get(f"/users/{owner_id}/photo").status_code, 403)

    def test_real_exports_follow_own_register_and_separate_management_permission(self):
        self.generate()
        self.login("teacher1")
        own_path = f"/worktime/export/{self.users['teacher1'].id}?year=2026&month=2&format=csv"
        other_path = f"/worktime/export/{self.users['teacher2'].id}?year=2026&month=2&format=csv"
        own = self.client.get(own_path)
        self.assertEqual(own.status_code, 200)
        self.assertIn(b"Minta teacher1", own.data)
        self.assertNotIn(b"Minta teacher2", own.data)
        self.assertEqual(self.client.get(other_path).status_code, 403)
        self.rule("worktime.index", "employee", False)
        self.assertEqual(self.client.get(own_path).status_code, 403)
        self.rule("worktime.management", "employee", True)
        own = self.client.get(own_path)
        other = self.client.get(other_path)
        self.assertEqual(own.status_code, 200)
        self.assertEqual(other.status_code, 200)
        self.assertIn(b"Minta teacher2", other.data)
        self.assertEqual(self.client.get("/worktime?year=2026&month=2").status_code, 403)
        self.rule("worktime.management", "employee", False)
        self.assertEqual(self.client.get(other_path).status_code, 403)
        self.assertEqual(self.client.get(own_path).status_code, 403)


if __name__ == "__main__":
    unittest.main()
