import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from urllib.parse import urlencode

from starlette.requests import Request

import app as app_module
import engine_web as engine
from push_notifications import PushNotificationService


def _sample_subscription(endpoint_suffix: str) -> dict:
    return {
        "endpoint": f"https://push.example/{endpoint_suffix}",
        "keys": {
            "p256dh": "test-p256dh",
            "auth": "test-auth",
        },
    }


class SlotPublishNotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_service = app_module.PUSH_SERVICE
        self.original_enabled = app_module.PUSH_SLOT_NOTIFICATIONS_ENABLED
        self.original_feature_waiting_board = app_module.FEATURE_WAITING_BOARD

        app_module.PUSH_SLOT_NOTIFICATIONS_ENABLED = True
        app_module.FEATURE_WAITING_BOARD = True
        app_module.PUSH_SERVICE = PushNotificationService(
            db_path=f"{self.tmpdir.name}/notifications.sqlite3",
            enabled=True,
            vapid_private_key="test-private",
            vapid_subject="mailto:test@example.com",
        )
        app_module.PUSH_SERVICE.init_db()
        engine.waiting_slots.clear()
        engine.slot_members.clear()

    def tearDown(self) -> None:
        engine.waiting_slots.clear()
        engine.slot_members.clear()
        app_module.PUSH_SERVICE.stop_worker()
        app_module.PUSH_SERVICE = self.original_service
        app_module.PUSH_SLOT_NOTIFICATIONS_ENABLED = self.original_enabled
        app_module.FEATURE_WAITING_BOARD = self.original_feature_waiting_board
        self.tmpdir.cleanup()

    def _request(self) -> Request:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/waiting/publish",
            "query_string": urlencode({}).encode("utf-8"),
            "headers": [],
        }
        return Request(scope)

    def _future_go_time(self, minutes: int) -> str:
        now_local = datetime.now(app_module.LOCAL_TZ).replace(second=0, microsecond=0)
        return (now_local + timedelta(minutes=minutes)).strftime("%H:%M")

    def _first_restaurant_id(self) -> str:
        restaurants = engine.list_restaurants()
        self.assertGreater(len(restaurants), 0)
        return restaurants[0].id

    def _register(self, endpoint_suffix: str, device_id: str, user_id: str | None) -> None:
        body = app_module.PushRegisterIn(
            subscription=_sample_subscription(endpoint_suffix),
            device_id=device_id,
            user_id=user_id,
            client_mode="standalone",
        )
        app_module.push_register(body)

    def test_first_publish_enqueues_event_and_excludes_self(self) -> None:
        rid = self._first_restaurant_id()
        self._register("publisher", "device-a", "alice")
        self._register("other1", "device-b", "bob")
        self._register("other2", "device-c", "carol")
        self._register("same-device", "device-a", "dave")

        response = app_module.waiting_publish_api(
            request=self._request(),
            body=app_module.PublishSlotIn(
                restaurant_id=rid,
                go_time=self._future_go_time(10),
                user_id="alice",
                device_id="device-a",
            ),
        )
        payload = json.loads(response.body.decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertIsNotNone(payload["notification_event_id"])
        self.assertEqual(payload["notifications_targeted"], 2)

        conn = sqlite3.connect(str(app_module.PUSH_SERVICE.db_path))
        event_count = conn.execute("SELECT COUNT(*) FROM push_events").fetchone()[0]
        queue_count = conn.execute("SELECT COUNT(*) FROM push_delivery_queue").fetchone()[0]
        conn.close()
        self.assertEqual(event_count, 1)
        self.assertEqual(queue_count, 2)

    def test_same_user_update_does_not_enqueue_new_event(self) -> None:
        rid = self._first_restaurant_id()
        self._register("other", "device-b", "bob")

        first = app_module.waiting_publish_api(
            request=self._request(),
            body=app_module.PublishSlotIn(
                restaurant_id=rid,
                go_time=self._future_go_time(10),
                user_id="alice",
                device_id="device-a",
            ),
        )
        first_payload = json.loads(first.body.decode("utf-8"))
        self.assertTrue(first_payload["ok"])
        self.assertIsNotNone(first_payload["notification_event_id"])

        second = app_module.waiting_publish_api(
            request=self._request(),
            body=app_module.PublishSlotIn(
                restaurant_id=rid,
                go_time=self._future_go_time(15),
                user_id="alice",
                device_id="device-a",
            ),
        )
        second_payload = json.loads(second.body.decode("utf-8"))
        self.assertTrue(second_payload["ok"])
        self.assertIsNone(second_payload["notification_event_id"])
        self.assertEqual(second_payload["notifications_targeted"], 0)

        conn = sqlite3.connect(str(app_module.PUSH_SERVICE.db_path))
        event_count = conn.execute("SELECT COUNT(*) FROM push_events").fetchone()[0]
        conn.close()
        self.assertEqual(event_count, 1)


if __name__ == "__main__":
    unittest.main()
