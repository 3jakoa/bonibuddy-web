import sqlite3
import tempfile
import unittest

from fastapi import HTTPException

import app as app_module
from push_notifications import PushNotificationService


def _sample_subscription(endpoint_suffix: str) -> dict:
    return {
        "endpoint": f"https://push.example/{endpoint_suffix}",
        "keys": {
            "p256dh": "test-p256dh",
            "auth": "test-auth",
        },
    }


class PushRegisterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.original_service = app_module.PUSH_SERVICE
        self.original_enabled = app_module.PUSH_SLOT_NOTIFICATIONS_ENABLED

        app_module.PUSH_SLOT_NOTIFICATIONS_ENABLED = True
        app_module.PUSH_SERVICE = PushNotificationService(
            db_path=f"{self.tmpdir.name}/notifications.sqlite3",
            enabled=True,
            vapid_private_key="test-private",
            vapid_subject="mailto:test@example.com",
        )
        app_module.PUSH_SERVICE.init_db()

    def tearDown(self) -> None:
        app_module.PUSH_SERVICE.stop_worker()
        app_module.PUSH_SERVICE = self.original_service
        app_module.PUSH_SLOT_NOTIFICATIONS_ENABLED = self.original_enabled
        self.tmpdir.cleanup()

    def test_register_valid_subscription_persists_row(self) -> None:
        body = app_module.PushRegisterIn(
            subscription=_sample_subscription("one"),
            device_id="device-one",
            user_id="Valid_User",
            client_mode="standalone",
        )
        res = app_module.push_register(body)

        self.assertTrue(res["ok"])
        self.assertIsInstance(res["subscription_id"], int)

        conn = sqlite3.connect(str(app_module.PUSH_SERVICE.db_path))
        row = conn.execute(
            "SELECT endpoint, device_id, user_id, status, client_mode FROM push_subscriptions WHERE id = ?",
            (res["subscription_id"],),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "https://push.example/one")
        self.assertEqual(row[1], "device-one")
        self.assertEqual(row[2], "valid_user")
        self.assertEqual(row[3], "active")
        self.assertEqual(row[4], "standalone")

    def test_register_invalid_subscription_returns_400(self) -> None:
        body = app_module.PushRegisterIn(
            subscription={"endpoint": "https://push.example/broken"},
            device_id="device-two",
            user_id="valid_user",
            client_mode="standalone",
        )
        with self.assertRaises(HTTPException) as ctx:
            app_module.push_register(body)
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
