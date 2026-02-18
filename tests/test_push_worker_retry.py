import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from push_notifications import PushNotificationService, PushPermanentError, PushTransientError


def _sample_subscription(endpoint_suffix: str) -> dict:
    return {
        "endpoint": f"https://push.example/{endpoint_suffix}",
        "keys": {
            "p256dh": "test-p256dh",
            "auth": "test-auth",
        },
    }


class PushWorkerRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.service = PushNotificationService(
            db_path=f"{self.tmpdir.name}/notifications.sqlite3",
            enabled=True,
            vapid_private_key="test-private",
            vapid_subject="mailto:test@example.com",
        )
        self.service.init_db()
        self.service.register_subscription(
            subscription=_sample_subscription("one"),
            device_id="device-one",
            user_id="bob",
            client_mode="standalone",
        )

    def tearDown(self) -> None:
        self.service.stop_worker()
        self.tmpdir.cleanup()

    def test_transient_failure_is_retried_then_delivered(self) -> None:
        enqueue = self.service.create_slot_published_event(
            restaurant_id="test-restaurant",
            go_time="12:00",
            publisher_user_id="alice",
            exclude_device_id=None,
        )
        self.assertIsNotNone(enqueue.event_id)
        self.assertEqual(enqueue.targeted, 1)

        calls = {"count": 0}

        def flaky_sender(_sub: dict, _payload: dict) -> None:
            calls["count"] += 1
            if calls["count"] == 1:
                raise PushTransientError("temporary outage")

        self.service.set_sender_for_tests(flaky_sender)
        t0 = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=1)

        stats1 = self.service.process_due_deliveries(now=t0)
        self.assertEqual(stats1["retried"], 1)
        self.assertEqual(stats1["sent"], 0)

        conn = sqlite3.connect(str(self.service.db_path))
        row1 = conn.execute(
            "SELECT status, attempt_count, next_attempt_at FROM push_delivery_queue WHERE event_id = ?",
            (enqueue.event_id,),
        ).fetchone()
        self.assertEqual(row1[0], "retry")
        self.assertEqual(row1[1], 1)

        t1 = t0 + timedelta(seconds=16)
        stats2 = self.service.process_due_deliveries(now=t1)
        self.assertEqual(stats2["sent"], 1)

        row2 = conn.execute(
            "SELECT status, attempt_count, delivered_at FROM push_delivery_queue WHERE event_id = ?",
            (enqueue.event_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(row2[0], "delivered")
        self.assertEqual(row2[1], 2)
        self.assertIsNotNone(row2[2])

    def test_permanent_failure_marks_subscription_inactive(self) -> None:
        enqueue = self.service.create_slot_published_event(
            restaurant_id="test-restaurant",
            go_time="13:00",
            publisher_user_id="alice",
            exclude_device_id=None,
        )
        self.assertIsNotNone(enqueue.event_id)
        self.assertEqual(enqueue.targeted, 1)

        def permanent_sender(_sub: dict, _payload: dict) -> None:
            raise PushPermanentError("subscription gone", deactivate_subscription=True)

        self.service.set_sender_for_tests(permanent_sender)
        stats = self.service.process_due_deliveries(now=datetime.now(timezone.utc))
        self.assertEqual(stats["failed_permanent"], 1)

        conn = sqlite3.connect(str(self.service.db_path))
        queue_row = conn.execute(
            "SELECT status FROM push_delivery_queue WHERE event_id = ?",
            (enqueue.event_id,),
        ).fetchone()
        sub_row = conn.execute("SELECT status FROM push_subscriptions WHERE endpoint = ?", ("https://push.example/one",)).fetchone()
        conn.close()
        self.assertEqual(queue_row[0], "failed_permanent")
        self.assertEqual(sub_row[0], "inactive")


if __name__ == "__main__":
    unittest.main()
