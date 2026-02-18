from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from contextlib import contextmanager
import logging
import sqlite3
import threading


logger = logging.getLogger("bonibuddy.push")


RETRY_BACKOFF_SECONDS = [15, 60, 300, 900, 3600, 21600, 86400]


class PushTransientError(Exception):
    pass


class PushPermanentError(Exception):
    def __init__(self, message: str, *, deactivate_subscription: bool = False):
        super().__init__(message)
        self.deactivate_subscription = deactivate_subscription


@dataclass(frozen=True)
class SlotPublishEnqueueResult:
    event_id: int | None
    targeted: int


class PushNotificationService:
    def __init__(
        self,
        *,
        db_path: str | Path,
        enabled: bool,
        vapid_private_key: str,
        vapid_subject: str,
        poll_interval_seconds: int = 5,
    ) -> None:
        self.db_path = Path(db_path)
        self.enabled = bool(enabled)
        self.vapid_private_key = (vapid_private_key or "").strip()
        self.vapid_subject = (vapid_subject or "").strip()
        self.poll_interval_seconds = max(int(poll_interval_seconds), 1)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._initialized = False
        self._sender: Callable[[dict[str, Any], dict[str, Any]], None] = self._send_with_webpush

    def set_sender_for_tests(self, sender: Callable[[dict[str, Any], dict[str, Any]], None]) -> None:
        self._sender = sender

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint TEXT NOT NULL UNIQUE,
                    p256dh TEXT NOT NULL,
                    auth TEXT NOT NULL,
                    device_id TEXT,
                    user_id TEXT,
                    client_mode TEXT NOT NULL DEFAULT 'standalone',
                    status TEXT NOT NULL DEFAULT 'active',
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS push_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    restaurant_id TEXT NOT NULL,
                    go_time TEXT NOT NULL,
                    publisher_user_id TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS push_delivery_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL,
                    subscription_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at TEXT NOT NULL,
                    last_error TEXT,
                    delivered_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, subscription_id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_push_subscriptions_status ON push_subscriptions(status)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_queue_due ON push_delivery_queue(status, next_attempt_at)"
            )
            conn.commit()
        self._initialized = True

    def start_worker(self) -> None:
        if not self.enabled:
            return
        self.init_db()
        with self._state_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._worker_loop, name="push-delivery-worker", daemon=True)
            self._thread.start()

    def stop_worker(self) -> None:
        self._stop_event.set()
        with self._state_lock:
            thread = self._thread
            self._thread = None
        if thread and thread.is_alive():
            thread.join(timeout=3)

    def register_subscription(
        self,
        *,
        subscription: dict[str, Any],
        device_id: str,
        user_id: str | None,
        client_mode: str,
    ) -> int:
        self._ensure_initialized()
        endpoint, p256dh, auth = self._extract_subscription_parts(subscription)
        device_id_norm = (device_id or "").strip()
        if not device_id_norm:
            raise ValueError("invalid_device_id")
        client_mode_norm = (client_mode or "").strip().lower()
        if client_mode_norm != "standalone":
            raise ValueError("invalid_client_mode")

        now_iso = self._utc_now().isoformat()
        user_norm = (user_id or "").strip().lower() or None

        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO push_subscriptions (
                    endpoint, p256dh, auth, device_id, user_id, client_mode, status, last_seen_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                ON CONFLICT(endpoint) DO UPDATE SET
                    p256dh=excluded.p256dh,
                    auth=excluded.auth,
                    device_id=excluded.device_id,
                    user_id=excluded.user_id,
                    client_mode=excluded.client_mode,
                    status='active',
                    last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at
                """,
                (
                    endpoint,
                    p256dh,
                    auth,
                    device_id_norm,
                    user_norm,
                    client_mode_norm,
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )
            row = conn.execute("SELECT id FROM push_subscriptions WHERE endpoint = ?", (endpoint,)).fetchone()
            conn.commit()
        if not row:
            raise RuntimeError("push_subscription_upsert_failed")
        sub_id = int(row["id"])
        logger.info("push_subscriptions_active subscription_id=%s", sub_id)
        return sub_id

    def unregister_subscription(self, *, device_id: str | None, endpoint: str | None) -> int:
        self._ensure_initialized()
        clauses = []
        values: list[Any] = []
        if device_id:
            clauses.append("device_id = ?")
            values.append(device_id)
        if endpoint:
            clauses.append("endpoint = ?")
            values.append(endpoint)
        if not clauses:
            raise ValueError("missing_identifier")
        now_iso = self._utc_now().isoformat()
        where_clause = " OR ".join(clauses)
        with self._db() as conn:
            cur = conn.execute(
                f"""
                UPDATE push_subscriptions
                SET status='inactive', updated_at=?
                WHERE status='active' AND ({where_clause})
                """,
                (now_iso, *values),
            )
            conn.commit()
            return int(cur.rowcount)

    def create_slot_published_event(
        self,
        *,
        restaurant_id: str,
        go_time: str,
        publisher_user_id: str,
        exclude_device_id: str | None,
    ) -> SlotPublishEnqueueResult:
        if not self.enabled:
            return SlotPublishEnqueueResult(event_id=None, targeted=0)
        self._ensure_initialized()

        now_iso = self._utc_now().isoformat()
        publisher_norm = (publisher_user_id or "").strip().lower()
        exclude_device_id_norm = (exclude_device_id or "").strip() or None

        with self._db() as conn:
            cur = conn.execute(
                """
                INSERT INTO push_events (event_type, restaurant_id, go_time, publisher_user_id, created_at)
                VALUES ('slot_published', ?, ?, ?, ?)
                """,
                (restaurant_id, go_time, publisher_norm or None, now_iso),
            )
            event_id = int(cur.lastrowid)

            query = [
                "SELECT id FROM push_subscriptions",
                "WHERE status = 'active'",
                "AND client_mode = 'standalone'",
            ]
            values: list[Any] = []
            if publisher_norm:
                query.append("AND (user_id IS NULL OR lower(user_id) != ?)")
                values.append(publisher_norm)
            if exclude_device_id_norm:
                query.append("AND (device_id IS NULL OR device_id != ?)")
                values.append(exclude_device_id_norm)
            sub_rows = conn.execute(" ".join(query), values).fetchall()

            targeted = 0
            for row in sub_rows:
                inserted = conn.execute(
                    """
                    INSERT OR IGNORE INTO push_delivery_queue (
                        event_id, subscription_id, status, attempt_count, next_attempt_at, last_error, delivered_at, created_at, updated_at
                    ) VALUES (?, ?, 'pending', 0, ?, NULL, NULL, ?, ?)
                    """,
                    (event_id, int(row["id"]), now_iso, now_iso, now_iso),
                )
                targeted += int(inserted.rowcount)
            conn.commit()

        logger.info("events_created event_id=%s targeted=%s", event_id, targeted)
        logger.info("deliveries_enqueued event_id=%s count=%s", event_id, targeted)
        return SlotPublishEnqueueResult(event_id=event_id, targeted=targeted)

    def process_due_deliveries(self, *, limit: int = 50, now: datetime | None = None) -> dict[str, int]:
        if not self.enabled:
            return {"sent": 0, "retried": 0, "failed_permanent": 0}
        self._ensure_initialized()

        now_dt = now or self._utc_now()
        now_iso = now_dt.isoformat()
        with self._db() as conn:
            rows = conn.execute(
                """
                SELECT
                    q.id AS queue_id,
                    q.attempt_count AS attempt_count,
                    q.subscription_id AS subscription_id,
                    s.endpoint AS endpoint,
                    s.p256dh AS p256dh,
                    s.auth AS auth,
                    e.restaurant_id AS restaurant_id,
                    e.go_time AS go_time
                FROM push_delivery_queue q
                INNER JOIN push_subscriptions s ON s.id = q.subscription_id
                INNER JOIN push_events e ON e.id = q.event_id
                WHERE q.status IN ('pending', 'retry')
                  AND q.next_attempt_at <= ?
                  AND s.status = 'active'
                ORDER BY q.next_attempt_at ASC, q.id ASC
                LIMIT ?
                """,
                (now_iso, max(int(limit), 1)),
            ).fetchall()

        stats = {"sent": 0, "retried": 0, "failed_permanent": 0}
        for row in rows:
            queue_id = int(row["queue_id"])
            subscription_id = int(row["subscription_id"])
            attempt_count = int(row["attempt_count"])
            subscription = {
                "endpoint": row["endpoint"],
                "keys": {
                    "p256dh": row["p256dh"],
                    "auth": row["auth"],
                },
            }
            payload = self._build_slot_payload(str(row["restaurant_id"]), str(row["go_time"]))
            try:
                self._sender(subscription, payload)
                self._mark_delivered(queue_id=queue_id, attempt_count=attempt_count, now=now_dt)
                stats["sent"] += 1
                logger.info("deliveries_sent queue_id=%s subscription_id=%s", queue_id, subscription_id)
            except PushPermanentError as exc:
                self._mark_permanent_failure(
                    queue_id=queue_id,
                    subscription_id=subscription_id,
                    attempt_count=attempt_count,
                    now=now_dt,
                    error=str(exc),
                    deactivate_subscription=exc.deactivate_subscription,
                )
                stats["failed_permanent"] += 1
                logger.info("deliveries_failed_permanent queue_id=%s reason=%s", queue_id, exc)
            except Exception as exc:
                if self._mark_retry(
                    queue_id=queue_id,
                    attempt_count=attempt_count,
                    now=now_dt,
                    error=str(exc),
                ):
                    stats["retried"] += 1
                    logger.info("deliveries_retried queue_id=%s", queue_id)
                else:
                    self._mark_permanent_failure(
                        queue_id=queue_id,
                        subscription_id=subscription_id,
                        attempt_count=attempt_count,
                        now=now_dt,
                        error=str(exc),
                        deactivate_subscription=False,
                    )
                    stats["failed_permanent"] += 1
                    logger.info("deliveries_failed_permanent queue_id=%s reason=%s", queue_id, exc)
        return stats

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.process_due_deliveries(limit=50)
            except Exception:
                logger.exception("push_worker_iteration_failed")
            self._stop_event.wait(self.poll_interval_seconds)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        self.init_db()

    def _mark_delivered(self, *, queue_id: int, attempt_count: int, now: datetime) -> None:
        now_iso = now.isoformat()
        with self._db() as conn:
            conn.execute(
                """
                UPDATE push_delivery_queue
                SET status='delivered',
                    attempt_count=?,
                    delivered_at=?,
                    updated_at=?,
                    last_error=NULL
                WHERE id=?
                """,
                (attempt_count + 1, now_iso, now_iso, queue_id),
            )
            conn.commit()

    def _mark_permanent_failure(
        self,
        *,
        queue_id: int,
        subscription_id: int,
        attempt_count: int,
        now: datetime,
        error: str,
        deactivate_subscription: bool,
    ) -> None:
        now_iso = now.isoformat()
        with self._db() as conn:
            conn.execute(
                """
                UPDATE push_delivery_queue
                SET status='failed_permanent',
                    attempt_count=?,
                    updated_at=?,
                    last_error=?
                WHERE id=?
                """,
                (attempt_count + 1, now_iso, (error or "")[:500], queue_id),
            )
            if deactivate_subscription:
                conn.execute(
                    "UPDATE push_subscriptions SET status='inactive', updated_at=? WHERE id=?",
                    (now_iso, subscription_id),
                )
            conn.commit()

    def _mark_retry(self, *, queue_id: int, attempt_count: int, now: datetime, error: str) -> bool:
        next_attempt_index = attempt_count
        if next_attempt_index >= len(RETRY_BACKOFF_SECONDS):
            return False
        delay_seconds = RETRY_BACKOFF_SECONDS[next_attempt_index]
        next_attempt_at = now + timedelta(seconds=delay_seconds)
        now_iso = now.isoformat()
        with self._db() as conn:
            conn.execute(
                """
                UPDATE push_delivery_queue
                SET status='retry',
                    attempt_count=?,
                    next_attempt_at=?,
                    updated_at=?,
                    last_error=?
                WHERE id=?
                """,
                (
                    attempt_count + 1,
                    next_attempt_at.isoformat(),
                    now_iso,
                    (error or "")[:500],
                    queue_id,
                ),
            )
            conn.commit()
        return True

    def _build_slot_payload(self, restaurant_id: str, go_time: str) -> dict[str, Any]:
        label = (restaurant_id or "").replace("_", " ").replace("-", " ").strip().title()
        return {
            "title": "BoniBuddy",
            "body": f"Nov slot: {label} ob {go_time}. Odpri app in se pridruži.",
            "url": "/feed",
        }

    def _send_with_webpush(self, subscription: dict[str, Any], payload: dict[str, Any]) -> None:
        if not (self.vapid_private_key and self.vapid_subject):
            raise PushTransientError("missing_vapid")
        try:
            from pywebpush import WebPushException, webpush  # type: ignore
        except Exception as exc:
            raise PushTransientError("pywebpush_import_failed") from exc

        def _raise_classified(exc: Exception) -> None:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                raise PushPermanentError(
                    f"webpush_subscription_gone_{status_code}",
                    deactivate_subscription=True,
                ) from exc
            raise PushTransientError(str(exc) or "webpush_failed") from exc

        try:
            webpush(
                subscription_info=subscription,
                data=self._to_json(payload),
                vapid_private_key=self.vapid_private_key,
                vapid_claims={"sub": self.vapid_subject},
                timeout=10,
            )
            return
        except TypeError:
            try:
                webpush(
                    subscription_info=subscription,
                    data=None,
                    vapid_private_key=self.vapid_private_key,
                    vapid_claims={"sub": self.vapid_subject},
                    timeout=10,
                )
                return
            except WebPushException as exc:
                _raise_classified(exc)
            except Exception as exc:
                _raise_classified(exc)
        except WebPushException as exc:
            _raise_classified(exc)
        except Exception as exc:
            _raise_classified(exc)

    @staticmethod
    def _to_json(payload: dict[str, Any]) -> str:
        import json

        return json.dumps(payload, ensure_ascii=False)

    @staticmethod
    def _extract_subscription_parts(subscription: dict[str, Any]) -> tuple[str, str, str]:
        if not isinstance(subscription, dict):
            raise ValueError("invalid_subscription")
        endpoint = (subscription.get("endpoint") or "").strip()
        keys = subscription.get("keys")
        if not isinstance(keys, dict):
            raise ValueError("invalid_subscription")
        p256dh = (keys.get("p256dh") or "").strip()
        auth = (keys.get("auth") or "").strip()
        if not endpoint or not p256dh or not auth:
            raise ValueError("invalid_subscription")
        return endpoint, p256dh, auth

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=3000")
        return conn

    @contextmanager
    def _db(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)
