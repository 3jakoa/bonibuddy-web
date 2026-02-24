import unittest
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi.responses import RedirectResponse
from starlette.requests import Request

import app as app_module
import engine_web as engine


class WaitingJoinBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        engine.waiting_slots.clear()
        engine.slot_members.clear()

    def tearDown(self) -> None:
        engine.waiting_slots.clear()
        engine.slot_members.clear()

    def _restaurant_ids(self) -> list[str]:
        ids = [r.id for r in engine.list_restaurants()]
        self.assertGreater(len(ids), 0, "No restaurants loaded for tests.")
        return ids

    def _two_restaurant_ids(self) -> tuple[str, str]:
        ids = self._restaurant_ids()
        self.assertGreaterEqual(len(ids), 2, "Need at least two restaurants for switch test.")
        return ids[0], ids[1]

    def _request(self, path: str, query: dict[str, str] | None = None, cookies: dict[str, str] | None = None) -> Request:
        headers = []
        if cookies:
            cookie_value = "; ".join(f"{k}={v}" for k, v in cookies.items())
            headers.append((b"cookie", cookie_value.encode("utf-8")))
        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "query_string": urlencode(query or {}).encode("utf-8"),
            "headers": headers,
        }
        return Request(scope)

    def test_recent_past_go_time_is_joinable(self) -> None:
        rid = self._restaurant_ids()[0]
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        target = now_utc - timedelta(minutes=10)
        res = engine.join_slot(user_id="recentpast_a", restaurant_id=rid, target_time=target)
        self.assertTrue(res.get("ok"), res)

    def test_expired_past_go_time_is_rejected(self) -> None:
        rid = self._restaurant_ids()[0]
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        target = now_utc - timedelta(minutes=app_module.ACTIVE_WINDOW_MINUTES)
        res = engine.join_slot(user_id="expiredpast_a", restaurant_id=rid, target_time=target)
        self.assertFalse(res.get("ok"), res)
        self.assertEqual(res.get("error"), "go_time_in_past")

    def test_future_go_time_is_joinable(self) -> None:
        rid = self._restaurant_ids()[0]
        now_utc = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        target = now_utc + timedelta(minutes=10)
        res = engine.join_slot(user_id="future_a", restaurant_id=rid, target_time=target)
        self.assertTrue(res.get("ok"), res)

    def test_publish_auto_switches_to_other_restaurant(self) -> None:
        rid_a, rid_b = self._two_restaurant_ids()
        now_local = datetime.now(app_module.LOCAL_TZ).replace(second=0, microsecond=0)
        go_a = (now_local + timedelta(minutes=10)).strftime("%H:%M")
        go_b = (now_local + timedelta(minutes=20)).strftime("%H:%M")

        published_a, err_a = app_module._publish_waiting_slot(
            restaurant_id=rid_a,
            go_time_raw=go_a,
            user_id_raw="autoswitch_a",
        )
        self.assertIsNone(err_a)
        self.assertIsNotNone(published_a)

        published_b, err_b = app_module._publish_waiting_slot(
            restaurant_id=rid_b,
            go_time_raw=go_b,
            user_id_raw="autoswitch_a",
        )
        self.assertIsNone(err_b)
        self.assertIsNotNone(published_b)

        membership = engine.get_user_membership("autoswitch_a")
        self.assertIsNotNone(membership)
        self.assertEqual(membership["restaurant_id"], rid_b)

        selected_a = app_module._parse_go_time(go_a, now_local=now_local)
        selected_b = app_module._parse_go_time(go_b, now_local=now_local)
        self.assertIsNotNone(selected_a)
        self.assertIsNotNone(selected_b)
        self.assertEqual(engine.get_waiting_count(rid_a, selected_a), 0)
        self.assertEqual(engine.get_waiting_count(rid_b, selected_b), 1)

    def test_same_restaurant_time_change_keeps_single_membership(self) -> None:
        rid = self._restaurant_ids()[0]
        now_local = datetime.now(app_module.LOCAL_TZ).replace(second=0, microsecond=0)
        go_a = (now_local + timedelta(minutes=10)).strftime("%H:%M")
        go_b = (now_local + timedelta(minutes=20)).strftime("%H:%M")

        published_a, err_a = app_module._publish_waiting_slot(
            restaurant_id=rid,
            go_time_raw=go_a,
            user_id_raw="retime_a",
        )
        self.assertIsNone(err_a)
        self.assertIsNotNone(published_a)

        published_b, err_b = app_module._publish_waiting_slot(
            restaurant_id=rid,
            go_time_raw=go_b,
            user_id_raw="retime_a",
        )
        self.assertIsNone(err_b)
        self.assertIsNotNone(published_b)

        slot = engine.waiting_slots[rid]
        members = engine.slot_members.get(slot.id, [])
        same_user_members = [m for m in members if m.user_id == "retime_a"]
        self.assertEqual(len(same_user_members), 1)

        selected_a = app_module._parse_go_time(go_a, now_local=now_local)
        selected_b = app_module._parse_go_time(go_b, now_local=now_local)
        self.assertIsNotNone(selected_a)
        self.assertIsNotNone(selected_b)
        self.assertEqual(engine.get_waiting_count(rid, selected_a), 0)
        self.assertEqual(engine.get_waiting_count(rid, selected_b), 1)

    def test_index_deeplink_accepts_recent_past_go_time(self) -> None:
        now_local = datetime.now(app_module.LOCAL_TZ).replace(second=0, microsecond=0)
        go_past = (now_local - timedelta(minutes=10)).strftime("%H:%M")
        request = self._request("/", query={"go_time": go_past})
        response = app_module.index(request)
        self.assertNotIsInstance(response, RedirectResponse)
        self.assertEqual(response.status_code, 200)

    def test_quick_join_accepts_recent_past_go_time(self) -> None:
        rid = self._restaurant_ids()[0]
        now_local = datetime.now(app_module.LOCAL_TZ).replace(second=0, microsecond=0)
        go_past = (now_local - timedelta(minutes=10)).strftime("%H:%M")

        published, err = app_module._publish_waiting_slot(
            restaurant_id=rid,
            go_time_raw=go_past,
            user_id_raw="quickjoin_owner",
        )
        self.assertIsNone(err)
        self.assertIsNotNone(published)

        request = self._request(
            f"/waiting/{rid}/quick-join",
            query={"go_time": go_past},
            cookies={"bb_uid": "quickjoin_joiner"},
        )
        response = app_module.waiting_quick_join(request=request, restaurant_id=rid, go_time=go_past)
        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.status_code, 303)
        location = response.headers.get("location", "")
        self.assertEqual(location, "https://instagram.com/quickjoin_owner")

        membership = engine.get_user_membership("quickjoin_joiner")
        self.assertIsNotNone(membership)
        self.assertEqual(membership["restaurant_id"], rid)

    def test_quick_join_without_cookie_redirects_to_instagram(self) -> None:
        rid = self._restaurant_ids()[0]
        now_local = datetime.now(app_module.LOCAL_TZ).replace(second=0, microsecond=0)
        go_time = (now_local + timedelta(minutes=10)).strftime("%H:%M")

        published, err = app_module._publish_waiting_slot(
            restaurant_id=rid,
            go_time_raw=go_time,
            user_id_raw="quickjoin_owner_no_cookie",
        )
        self.assertIsNone(err)
        self.assertIsNotNone(published)

        request = self._request(
            f"/waiting/{rid}/quick-join",
            query={"go_time": go_time},
        )
        response = app_module.waiting_quick_join(request=request, restaurant_id=rid, go_time=go_time)
        self.assertIsInstance(response, RedirectResponse)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers.get("location", ""), "https://instagram.com/quickjoin_owner_no_cookie")


if __name__ == "__main__":
    unittest.main()
