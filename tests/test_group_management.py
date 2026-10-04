"""Standalone group editing, scoped assignments and optional shift selection."""
import unittest
from unittest.mock import patch
from sqlalchemy import text

import app.worktime as group_routes

import test_worktime_routes as fixtures
from app import db
from app.worktime_models import WorkAssignment, WorkGroup
from app.worktime_service import build_payload, settings_revision


class GroupManagementTests(unittest.TestCase):
    setUp = fixtures.WorktimeRoutesTests.setUp
    tearDown = fixtures.WorktimeRoutesTests.tearDown
    login = fixtures.WorktimeRoutesTests.login

    def group_form(self, group=None, **values):
        data = {"csrf_token": "test-worktime-csrf", "place_id": str(self.site.id),
                "settings_revision": settings_revision(self.site.id), **values}
        path = f"/groups/{group.id}/edit" if group else "/groups/new"
        return self.client.post(path, data=data, headers={"Accept": "application/json"})

    def test_list_filters_workplace_and_legacy_redirects(self):
        response = self.client.get(f"/groups?place_id={self.site.id}")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Group A", html)
        self.assertIn("Minta teacher1", html)
        self.assertIn(f'/groups/{self.groups[0].id}/edit', html)
        self.assertIn('/groups/new?place_id=', html)
        other = self.client.get(f"/groups?place_id={self.other_site.id}").get_data(as_text=True)
        self.assertNotIn("Group A", other)
        self.assertNotIn("Minta teacher1", other)
        legacy = self.client.get(f"/worktime/groups?place_id={self.site.id}&year=2026&month=2")
        self.assertEqual(legacy.status_code, 302)
        self.assertEqual(legacy.location, f"/groups?place_id={self.site.id}")

    def test_create_edit_stale_duplicate_and_csrf_without_month_fields(self):
        old = settings_revision(self.site.id)
        response = self.group_form(action="group", name="Group C", start_date="2026-01-01")
        self.assertEqual(response.status_code, 200, response.json)
        group = WorkGroup.query.filter_by(name="Group C").one()
        edited = self.group_form(group, action="group", name="Group C renamed", start_date="2026-01-01")
        self.assertEqual(edited.status_code, 200)
        stale = self.group_form(group, action="group", name="Stale replacement", start_date="2026-01-01", settings_revision=old)
        self.assertEqual(stale.status_code, 409)
        db.session.refresh(group)
        self.assertEqual(group.name, "Group C renamed")
        self.assertEqual(self.group_form(group, action="group", name="No CSRF", start_date="2026-01-01", csrf_token="wrong").status_code, 400)
        self.assertEqual(self.group_form(group, action="group", name="Group A", start_date="2026-01-01").status_code, 409)
        db.session.refresh(group)
        self.assertEqual(group.name, "Group C renamed")

    def test_flexible_then_fixed_assignment_and_overlap_protection(self):
        group = self.groups[0]
        data = dict(action="assignment", contract_id=str(self.contracts["assistant"].id), start_date="2026-02-01", shift_phase="")
        before_revision = settings_revision(self.site.id)
        before_fingerprint = build_payload(self.site.id, 2026, 2)[1]
        response = self.group_form(group, **data)
        self.assertEqual(response.status_code, 200, response.json)
        assignment = WorkAssignment.query.filter_by(contract_id=self.contracts["assistant"].id).one()
        self.assertTrue(assignment.flexible_shift)
        self.assertNotEqual(before_revision, settings_revision(self.site.id))
        self.assertNotEqual(before_fingerprint, build_payload(self.site.id, 2026, 2)[1])
        payload, _ = build_payload(self.site.id, 2026, 2)
        worker = next(row for row in payload["workers"] if row["contract_id"] == assignment.contract_id)
        self.assertIsNone(worker["assignments"][0]["shift_phase"])
        self.assertEqual(self.group_form(self.groups[1], **data).status_code, 400)
        data.update(assignment_id=str(assignment.id), shift_phase="1")
        response = self.group_form(group, **data)
        self.assertEqual(response.status_code, 200, response.json)
        db.session.refresh(assignment)
        self.assertFalse(assignment.flexible_shift)
        self.assertEqual(assignment.shift_phase, 1)
        self.assertEqual(self.group_form(group, **{**data, "shift_phase": "2"}).status_code, 400)
        self.assertEqual(self.group_form(group, **{**data, "start_date": "2024-01-01"}).status_code, 400)
        self.assertEqual(self.group_form(group, action="delete_assignment", assignment_id=assignment.id).status_code, 200)
        self.assertIsNone(db.session.get(WorkAssignment, assignment.id))

    def test_editor_cannot_modify_or_remove_another_groups_assignment(self):
        assignment = WorkAssignment.query.filter_by(contract_id=self.contracts["teacher3"].id).one()
        original_group_id = assignment.group_id
        response = self.group_form(self.groups[0], action="assignment", assignment_id=str(assignment.id),
                                   contract_id=str(assignment.contract_id), start_date="2025-01-01", shift_phase="")
        self.assertEqual(response.status_code, 404)
        response = self.group_form(self.groups[0], action="delete_assignment", assignment_id=str(assignment.id))
        self.assertEqual(response.status_code, 404)
        db.session.refresh(assignment)
        self.assertEqual(assignment.group_id, original_group_id)
        self.assertFalse(assignment.flexible_shift)
        self.assertEqual(self.group_form(self.groups[0], action="group", name="Wrong site", start_date="2025-01-01", place_id=str(self.other_site.id)).status_code, 404)

    def test_invalid_html_preserves_inputs_and_does_not_write(self):
        group = self.groups[0]
        data = {"csrf_token": "test-worktime-csrf", "place_id": self.site.id,
                "settings_revision": settings_revision(self.site.id), "action": "group",
                "name": "Retained group name", "start_date": "2026-03-01"}
        response = self.client.post(f"/groups/{group.id}/edit", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn('value="Retained group name"', response.get_data(as_text=True))
        db.session.refresh(group)
        self.assertEqual(group.name, "Group A")
        data.update(action="assignment", contract_id=self.contracts["teacher1"].id, start_date="2026-02-01", shift_phase="")
        response = self.client.post(f"/groups/{group.id}/edit", data=data)
        self.assertEqual(response.status_code, 400)
        html = response.get_data(as_text=True)
        self.assertIn('value="2026-02-01"', html)
        self.assertIn('No assigned shift', html)

    def test_roles_and_read_only_get(self):
        original = settings_revision(self.site.id)
        paths = [f"/groups?place_id={self.site.id}", f"/groups/new?place_id={self.site.id}", f"/groups/{self.groups[0].id}/edit"]
        for name in ("hr", "ceo", "teacher1", "developer"):
            self.login(name)
            for path in paths:
                self.assertEqual(self.client.get(path).status_code, 200 if name in ("hr", "ceo") else 403)
            if name not in ("hr", "ceo"):
                self.assertEqual(self.group_form(self.groups[0], action="group", name="Forbidden", start_date="2025-01-01").status_code, 403)
        self.assertEqual(settings_revision(self.site.id), original)

    def test_edit_detects_concurrent_change_after_group_was_loaded_before_workplace_lock(self):
        group = self.groups[0]
        original_place = group_routes._place
        data = {"csrf_token": "test-worktime-csrf", "place_id": str(self.site.id),
                "settings_revision": settings_revision(self.site.id), "action": "group",
                "name": "Old window overwrite", "start_date": "2025-01-01"}

        def concurrent_place(*args, **kwargs):
            # Emulate a second manager committing while this request waits for
            # the workplace lock, after the ORM already loaded the old group.
            with db.engine.begin() as connection:
                connection.execute(text("UPDATE work_groups SET name = :name WHERE id = :id"),
                                   {"name": "Concurrent manager name", "id": group.id})
            return original_place(*args, **kwargs)

        with patch("app.worktime._place", side_effect=concurrent_place):
            response = self.client.post(f"/groups/{group.id}/edit", data=data,
                                        headers={"Accept": "application/json"})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        db.session.refresh(group)
        self.assertEqual(group.name, "Concurrent manager name")
