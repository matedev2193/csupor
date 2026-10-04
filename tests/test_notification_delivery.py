"""Delivery timing, durable retries, privacy and concurrent-worker regressions."""

import os
import tempfile
import threading
import unittest
import uuid
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask

from app import create_app, db
from app.mail_settings import MailDeliveryError
from app.models import (
    Contract, ContractType, LegalEntity, LeaveRequest, LeaveRequestCategory,
    LeaveRequestStatus, PlaceOfWork, User,
)
from app.notification_delivery import (
    _group_due_notifications, _worker_loop, init_notifications,
    notification_due_at, notification_is_urgent, process_due_notifications,
    wake_notifications,
)
from app.notification_models import LeaveNotification, MailBatch


class NotificationTimingTests(unittest.TestCase):
    def test_budapest_20_in_summer_and_winter(self):
        for now, expected in [
            (datetime(2026, 7, 4, 10, tzinfo=timezone.utc), datetime(2026, 7, 4, 18)),
            (datetime(2026, 12, 4, 10, tzinfo=timezone.utc), datetime(2026, 12, 4, 19)),
        ]:
            self.assertEqual(notification_due_at(date(2027, 1, 30), now), expected)

    def test_20_boundary_and_after_20_next_day(self):
        exact = datetime(2026, 7, 4, 18, tzinfo=timezone.utc)
        self.assertEqual(notification_due_at(date(2027, 1, 30), exact), exact.replace(tzinfo=None))
        self.assertEqual(notification_due_at(date(2027, 1, 30), exact + timedelta(microseconds=1)), datetime(2026, 7, 5, 18))

    def test_dst_transition_next_day_uses_local_clock(self):
        for now, expected in [
            (datetime(2026, 3, 28, 20, tzinfo=timezone.utc), datetime(2026, 3, 29, 18)),
            (datetime(2026, 10, 24, 20, tzinfo=timezone.utc), datetime(2026, 10, 25, 19)),
        ]:
            self.assertEqual(notification_due_at(date(2027, 1, 30), now), expected)

    def test_exact_24_hours_and_already_started_urgent(self):
        now = datetime(2026, 7, 4, 22, tzinfo=timezone.utc)  # Budapest July 5 midnight.
        self.assertTrue(notification_is_urgent(date(2026, 7, 6), now))
        self.assertFalse(notification_is_urgent(date(2026, 7, 6), now - timedelta(microseconds=1)))
        self.assertEqual(notification_due_at(date(2026, 7, 6), now), now.replace(tzinfo=None))
        self.assertTrue(notification_is_urgent(date(2020, 1, 1), now))


class NotificationWorkerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config.update(SQLALCHEMY_DATABASE_URI="sqlite://", EMAIL_WORKER_ENABLED=True)
        db.init_app(self.app)

    def tearDown(self):
        with self.app.app_context():
            db.engine.dispose()

    def test_init_wakes_worker_without_waiting_for_a_web_request(self):
        with patch("app.notification_delivery.wake_notifications") as wake, \
                patch("app.notification_delivery.os.register_at_fork", create=True):
            init_notifications(self.app)
        wake.assert_called_once_with(self.app)

    def test_fork_replaces_worker_lock_and_pool_without_closing_parent_sockets(self):
        with patch("app.notification_delivery.wake_notifications"), \
                patch("app.notification_delivery.os.register_at_fork", create=True) as register:
            init_notifications(self.app)
        callback = register.call_args.kwargs["after_in_child"]
        parent_state = self.app.extensions["leave_notification_worker"]
        parent_lock = parent_state["lock"]
        parent_state.update(pid=123, thread=object())
        parent_lock.acquire()
        try:
            with self.app.app_context():
                engine = db.engine
            with patch.object(engine, "dispose") as dispose:
                callback()
            dispose.assert_called_once_with(close=False)
            child_state = self.app.extensions["leave_notification_worker"]
            self.assertIsNot(child_state["lock"], parent_lock)
            self.assertNotIn("thread", child_state)
            self.assertNotIn("pid", child_state)
            self.assertTrue(child_state["lock"].acquire(blocking=False))
            child_state["lock"].release()
        finally:
            parent_lock.release()

    def test_worker_processes_startup_and_poll_without_web_requests(self):
        class StopWorker(BaseException):
            pass

        wake = SimpleNamespace(wait=unittest.mock.Mock(side_effect=[False, StopWorker()]),
                               clear=unittest.mock.Mock())
        with patch("app.notification_delivery.process_due_notifications") as process:
            with self.assertRaises(StopWorker):
                _worker_loop(self.app, wake)
        self.assertEqual(process.call_count, 2)
        process.assert_called_with(self.app)
        wake.wait.assert_called_with(timeout=30)
        wake.clear.assert_called_once()


class NotificationDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {
            "DATABASE_URL": "sqlite:///" + self.tempdir.name + "/mail.sqlite",
            "EMAIL_WORKER_ENABLED": "false", "SECRET_KEY": "notification-test-only",
        }):
            self.app = create_app()
        self.app.config.update(TESTING=True, EMAIL_WORKER_ENABLED=False)
        self.context = self.app.app_context()
        self.context.push()
        # Models are imported before app creation, but keep this focused test
        # compatible with an app that has not enabled notifications yet.
        db.create_all()
        self.owner = User(username="Owner", email="owner@example.invalid", password_hash="unused")
        self.manager = User(username="Manager", email="manager@example.invalid", password_hash="unused")
        entity = LegalEntity(name="Entity", address="Example", om_id="000001", tax_number="00000000001")
        place = PlaceOfWork(legal_entity=entity, address="Example")
        contract = Contract(user=self.owner, employer=entity, place_of_work=place,
                            contract_type=ContractType.teacher, start_date=date(2020, 1, 1),
                            job_title="Teacher", working_hours_per_week=40)
        self.leave = LeaveRequest(user=self.owner, contract=contract, category=LeaveRequestCategory.health_leave,
                                  start_date=date(2026, 11, 20), end_date=date(2026, 11, 21),
                                  status=LeaveRequestStatus.pending_approval, note="PRIVATE MEDICAL NOTE")
        db.session.add_all([self.leave, self.manager])
        db.session.commit()
        self.now = datetime(2026, 10, 4, 18, tzinfo=timezone.utc)
        self.patches = [
            patch("app.mail_settings.is_mail_enabled", return_value=True),
            patch("app.mail_settings.get_mail_settings", return_value=SimpleNamespace(base_url="https://portal.example.invalid")),
            patch("app.mail_settings.send_email"),
            patch("app.notification_events.notification_is_actionable", return_value=True),
        ]
        self.enabled, self.settings, self.send, self.actionable = [p.start() for p in self.patches]

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.tempdir.cleanup()

    def queue(self, recipient=None, **overrides):
        recipient = recipient or self.owner
        fields = dict(
            recipient_id=recipient.id, leave_request_id=self.leave.id, event_key=uuid.uuid4().hex,
            event_type="modified", is_owner=recipient.id == self.owner.id, is_task=False,
            urgent=False, created_at=self.now.replace(tzinfo=None) - timedelta(hours=2),
            due_at=self.now.replace(tzinfo=None), payload={
                "applicant_name": "Owner", "start_date": "2026-11-20", "end_date": "2026-11-21",
                "status": "approved", "note": "PAYLOAD PRIVATE MEDICAL NOTE",
            },
        )
        fields.update(overrides)
        row = LeaveNotification(**fields)
        db.session.add(row)
        db.session.commit()
        return row

    def test_no_send_before_20_then_one_digest_for_all_own_changes(self):
        self.queue(event_type="approved")
        self.queue(event_type="modified")
        self.assertEqual(process_due_notifications(now=self.now - timedelta(seconds=1))["sent"], 0)
        self.assertEqual(process_due_notifications(now=self.now)["sent"], 1)
        self.assertEqual(process_due_notifications(now=self.now + timedelta(hours=1))["sent"], 0)
        self.assertEqual(self.send.call_count, 1)
        args = self.send.call_args.args
        self.assertEqual(args[0], "owner@example.invalid")
        self.assertIn("elfogadták", args[2])
        self.assertIn("módosították", args[2])
        self.assertIn("https://portal.example.invalid/leaves", args[2])
        self.assertNotIn("PRIVATE", args[2] + args[3])
        self.assertEqual(LeaveNotification.query.filter_by(status="sent").count(), 2)

    def test_overdue_digest_is_processed_after_worker_restart(self):
        self.queue()
        self.assertEqual(process_due_notifications(now=self.now + timedelta(days=2))["sent"], 1)
        self.assertEqual(MailBatch.query.one().batch_key, f"digest:{self.owner.id}:2026-10-04")

    def test_urgent_not_held_until_20(self):
        self.queue(urgent=True, due_at=self.now.replace(tzinfo=None) - timedelta(hours=3))
        self.assertEqual(process_due_notifications(now=self.now - timedelta(hours=2))["sent"], 1)
        self.assertIn("sürgős", self.send.call_args.args[1])

    def test_recipient_current_email_and_html_escape(self):
        self.queue(recipient=self.manager, is_owner=False, is_task=True,
                   payload={"applicant_name": '<script>"unsafe"</script>', "start_date": "2026-11-20"})
        self.manager.email = "new@example.invalid"
        db.session.commit()
        process_due_notifications(now=self.now)
        args = self.send.call_args.args
        self.assertEqual(args[0], "new@example.invalid")
        self.assertNotIn("<script>", args[3])
        self.assertIn("&lt;script&gt;", args[3])

    def test_stale_tasks_skipped_without_email(self):
        self.queue(recipient=self.manager, is_owner=False, is_task=True)
        self.actionable.return_value = False
        result = process_due_notifications(now=self.now)
        self.assertEqual(result["skipped"], 1)
        self.send.assert_not_called()
        self.assertEqual(LeaveNotification.query.one().status, "skipped")

    def test_latest_task_only_but_preserves_own_change(self):
        self.queue(is_owner=True, is_task=True, event_type="approved")
        self.queue(is_owner=False, is_task=True, event_type="modified",
                   created_at=self.now.replace(tzinfo=None) - timedelta(hours=1))
        process_due_notifications(now=self.now)
        body = self.send.call_args.args[2]
        self.assertEqual(body.count("Jóváhagyás szükséges."), 1)
        self.assertIn("elfogadták", body)
        self.assertEqual(LeaveNotification.query.filter_by(status="sent").count(), 2)

    def test_owner_change_survives_task_authority_loss(self):
        self.queue(is_owner=True, is_task=True)
        self.actionable.return_value = False
        process_due_notifications(now=self.now)
        self.assertIn("Saját távolléti igényeid", self.send.call_args.args[2])
        self.assertNotIn("Elintézendő", self.send.call_args.args[2])

    def test_newer_sent_urgent_task_supersedes_older_digest_task(self):
        self.queue(recipient=self.manager, is_owner=False, is_task=True)
        newer = self.queue(recipient=self.manager, is_owner=False, is_task=True, urgent=True,
                           created_at=self.now.replace(tzinfo=None) - timedelta(hours=1),
                           due_at=self.now.replace(tzinfo=None) - timedelta(hours=1))
        self.assertEqual(process_due_notifications(now=self.now - timedelta(minutes=30))["sent"], 1)
        self.assertEqual(process_due_notifications(now=self.now)["skipped"], 1)
        self.assertEqual(self.send.call_count, 1)
        self.assertEqual(db.session.get(LeaveNotification, newer.id).status, "sent")

    def test_never_send_owner_update_to_another_user(self):
        self.queue(recipient=self.manager, is_owner=True, is_task=False)
        process_due_notifications(now=self.now)
        self.send.assert_not_called()

    def test_disabled_smtp_keeps_queue_without_claiming(self):
        self.queue()
        self.enabled.return_value = False
        self.assertTrue(process_due_notifications(now=self.now)["disabled"])
        self.assertEqual(MailBatch.query.count(), 0)
        self.assertEqual(LeaveNotification.query.one().status, "pending")
        self.enabled.return_value = True
        self.assertEqual(process_due_notifications(now=self.now)["sent"], 1)

    def test_failure_keeps_immutable_batch_and_retries_same_message_id(self):
        self.queue()
        self.send.side_effect = MailDeliveryError("connection")
        self.assertEqual(process_due_notifications(now=self.now)["failed"], 1)
        original_id = self.send.call_args.kwargs["message_id"]
        batch = MailBatch.query.one()
        self.assertEqual(batch.attempts, 1)
        self.assertEqual(batch.last_error, "connection")
        self.assertEqual(LeaveNotification.query.one().status, "pending")
        self.assertEqual(process_due_notifications(now=self.now + timedelta(minutes=1))["sent"], 0)
        # A late commit in the already-frozen daily slot goes into tomorrow's
        # digest rather than changing the message being retried.
        late = self.queue()
        self.send.side_effect = None
        self.assertEqual(process_due_notifications(now=self.now + timedelta(minutes=6))["sent"], 1)
        self.assertEqual(self.send.call_args.kwargs["message_id"], original_id)
        db.session.refresh(late)
        self.assertEqual(late.status, "pending")
        self.assertEqual(late.due_at, datetime(2026, 10, 5, 18))
        self.assertEqual(MailBatch.query.one().attempts, 2)

    def test_raw_smtp_errors_never_persist(self):
        self.queue()
        self.send.side_effect = RuntimeError("PASSWORD email@example.invalid MEDICAL")
        process_due_notifications(now=self.now)
        self.assertEqual(MailBatch.query.one().last_error, "internal")

    def test_abandoned_claim_recovered_after_lease(self):
        self.queue()
        _group_due_notifications(self.now.replace(tzinfo=None))
        batch = MailBatch.query.one()
        batch.status = "sending"
        batch.claimed_at = self.now.replace(tzinfo=None) - timedelta(minutes=11)
        batch.claim_token = uuid.uuid4().hex
        batch.attempts = 1
        db.session.commit()
        self.assertEqual(process_due_notifications(now=self.now)["sent"], 1)
        self.assertEqual(MailBatch.query.one().attempts, 2)

    def test_concurrent_workers_only_one_sends_claimed_batch(self):
        self.queue()
        _group_due_notifications(self.now.replace(tzinfo=None))
        entered, release = threading.Event(), threading.Event()
        failures = []

        def send(*args, **kwargs):
            entered.set()
            release.wait(timeout=5)

        def run():
            try:
                process_due_notifications(self.app, now=self.now)
            except Exception as exc:
                failures.append(exc)

        self.send.side_effect = send
        first = threading.Thread(target=run)
        first.start()
        self.assertTrue(entered.wait(timeout=5))
        second = threading.Thread(target=run)
        second.start()
        second.join(timeout=5)
        release.set()
        first.join(timeout=5)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.send.call_count, 1)

    def test_user_and_leave_deletion_cascade_notifications(self):
        self.queue()
        _group_due_notifications(self.now.replace(tzinfo=None))
        db.session.delete(self.leave)
        db.session.commit()
        self.assertEqual(LeaveNotification.query.count(), 0)
        db.session.delete(self.owner)
        db.session.commit()
        self.assertEqual(MailBatch.query.count(), 0)

    def test_worker_start_failure_does_not_fail_committed_business_request(self):
        row = self.queue()
        self.app.config["EMAIL_WORKER_ENABLED"] = True
        self.app.extensions["leave_notification_worker"] = {"lock": threading.Lock()}
        with patch("app.notification_delivery.threading.Thread.start", side_effect=RuntimeError("cannot start")):
            wake_notifications(self.app)
        self.assertEqual(db.session.get(LeaveNotification, row.id).status, "pending")
        self.app.config["EMAIL_WORKER_ENABLED"] = False


if __name__ == "__main__":
    unittest.main()
