"""Transactional outbox delivery with honest ambiguous-send handling."""
from __future__ import annotations

import json
import secrets
import sqlite3
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .config import Config
from .db import transaction
from .research_sweeps import LEAD_POLICY, _later
from .store import now, safe_url


def private_secrets(config: Config) -> dict:
    path = config.data_dir / "secrets.toml"
    if not path.exists():
        return {}
    if path.stat().st_mode & 0o077:
        raise PermissionError("Secrets file must be owner-only (chmod 600)")
    return tomllib.loads(path.read_text())


def claim(db: sqlite3.Connection, config: Config) -> sqlite3.Row | None:
    with transaction(db):
        current = now()
        db.execute("UPDATE notification_events SET status='DELIVERY_UNKNOWN',last_error='worker stopped during send' WHERE status='SENDING' AND lease_until<?", (current,))
        db.execute("UPDATE notification_events SET status='EXPIRED',suppression_reason='auction deadline passed while delivery was uncertain' WHERE status='DELIVERY_UNKNOWN' AND expires_at IS NOT NULL AND expires_at<=?", (current,))
        db.execute("UPDATE notification_events SET status='QUEUED' WHERE status='DELIVERY_UNKNOWN' AND attempts<2 AND lease_until IS NOT NULL AND lease_until<=?", (current,))
        row = db.execute("SELECT * FROM notification_events WHERE status='QUEUED' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        # Delivery is the final safety boundary. Old queued lead/discovery
        # events can never escape when notifications are re-enabled.
        if row["stage"] not in {"BARGAIN_FIND", "PHOTO_REVIEW_REPLY"}:
            db.execute("UPDATE notification_events SET status='SUPPRESSED',suppression_reason='research-first collector policy' WHERE id=?", (row["id"],))
            return None
        if row["expires_at"] and row["expires_at"] <= current:
            db.execute("UPDATE notification_events SET status='EXPIRED',suppression_reason='auction deadline passed' WHERE id=?", (row["id"],))
            return None
        payload = json.loads(row["payload_json"])
        if row["stage"] == "PHOTO_REVIEW_REPLY":
            review = db.execute("SELECT 1 FROM reviews WHERE id=? AND listing_id=? AND policy_hash='telegram-photo-review-v1' AND status='DONE'",
                                (payload.get("review_id"), row["listing_id"])).fetchone()
            valid = bool(review and row["channel"] == "telegram" and type(payload.get("reply_to_message_id")) is int)
        else:
            decision = db.execute("SELECT action FROM user_decisions WHERE listing_id=? AND undone_at IS NULL ORDER BY id DESC LIMIT 1", (row["listing_id"],)).fetchone()
            if decision and decision[0] in {"DISMISS", "BOUGHT", "OWNED"}:
                db.execute("UPDATE notification_events SET status='SUPPRESSED',suppression_reason='owner decision' WHERE id=?", (row["id"],))
                return None
            review = db.execute("SELECT r.*,l.current_observation_id,l.imported,l.platform,l.canonical_url,l.availability,o.auction_end_at,o.captured_at,o.available FROM reviews r JOIN listings l ON l.id=r.listing_id JOIN observations o ON o.id=l.current_observation_id WHERE r.id=? AND r.listing_id=?",
                                (payload.get("review_id"), row["listing_id"])).fetchone()
            if review and review["platform"] == "ebay":
                live = db.execute("SELECT * FROM live_checks WHERE listing_id=? AND observation_id=? AND provider='ebay_browse' ORDER BY checked_at DESC LIMIT 1",
                                  (row["listing_id"], review["observation_id"])).fetchone()
                source_current = bool(live and live["availability"] == "LIVE" and live["checked_at"] >= _later(-1800)
                                      and live["price_minor"] == payload.get("price_minor")
                                      and live["shipping_minor"] == payload.get("shipping_minor")
                                      and live["shipping_currency"] == payload.get("shipping_currency"))
            else:
                source_current = bool(review and review["canonical_url"]
                                      and safe_url(review["canonical_url"]) == review["canonical_url"]
                                      and review["captured_at"] >= _later(-7200)
                                      and review["available"] not in {"False", "false", "0"}
                                      and review["availability"] not in {"ENDED", "UNAVAILABLE"})
            try:
                screen = json.loads(review["result_json"] or "{}").get("bargain_screen", {}) if review else {}
            except (json.JSONDecodeError, TypeError, AttributeError):
                screen = {}
            valid = bool(review and review["policy_hash"] == LEAD_POLICY and review["status"] == "DONE"
                         and review["verdict"] in {"GEM", "UNICORN"}
                         and screen.get("accepted") is True
                         and review["observation_id"] == review["current_observation_id"] and not review["imported"]
                         and (not review["auction_end_at"] or review["auction_end_at"] > current)
                         and source_current)
        if not valid:
            db.execute("UPDATE notification_events SET status='SUPPRESSED',suppression_reason='research or current listing check no longer valid' WHERE id=?", (row["id"],))
            return None
        token = secrets.token_hex(16)
        until = (datetime.now(timezone.utc) + timedelta(seconds=90)).isoformat(timespec="seconds").replace("+00:00", "Z")
        db.execute("UPDATE notification_events SET status='SENDING',lease_token=?,lease_until=?,attempts=attempts+1 WHERE id=?", (token, until, row["id"]))
        return db.execute("SELECT * FROM notification_events WHERE id=?", (row["id"],)).fetchone()


def send_one(db: sqlite3.Connection, config: Config, *, fake: bool = False) -> bool:
    if not fake and not (config.production and config.allow_real_notifications and config.notification_enabled):
        return False
    event = claim(db, config)
    if event is None:
        return False
    payload = json.loads(event["payload_json"])
    try:
        if fake:
            response = {"status": 1, "request": "FAKE-REPLAY-ONLY"}
        else:
            secret = private_secrets(config)
            if event["channel"] == "telegram":
                token = secret.get("telegram", {}).get("bot_token")
                chat_id = secret.get("telegram", {}).get("chat_id")
                if not token or not chat_id:
                    raise ValueError("Telegram bot token and chat ID are missing")
                url = str(payload.get("url") or "")
                possible_repeat = "Possible duplicate: an earlier send had no confirmed result.\n" if event["attempts"] > 1 else ""
                message = {"chat_id": str(chat_id), "text": (possible_repeat + str(payload.get("title") or "Photobook Radar") + "\n" + str(payload.get("message") or ""))[:4000], "disable_web_page_preview": True}
                if type(payload.get("reply_to_message_id")) is int:
                    message["reply_parameters"] = {"message_id": payload["reply_to_message_id"], "allow_sending_without_reply": True}
                if url.startswith("https://"):
                    buttons = [{"text": "Open listing", "url": url}]
                    comp_url = str(payload.get("comp_url") or "")
                    if comp_url.startswith("https://") and safe_url(comp_url) == comp_url:
                        buttons.append({"text": "Sold comp", "url": comp_url})
                    message["reply_markup"] = {"inline_keyboard": [buttons]}
                with httpx.Client(timeout=15, follow_redirects=False) as client:
                    reply = client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=message)
                reply.raise_for_status()
                body = reply.json()
                if body.get("ok") is not True:
                    raise RuntimeError("Telegram rejected the message")
                response = {"request": str(body.get("result", {}).get("message_id")), "status": 1}
            elif event["channel"] == "pushover":
                app_token = secret.get("pushover", {}).get("app_token")
                user_key = secret.get("pushover", {}).get("user_key")
                if not app_token or not user_key:
                    raise ValueError("Pushover credentials are missing")
                possible_repeat = "Possible duplicate: earlier send result unknown. " if event["attempts"] > 1 else ""
                message = {
                    "token": app_token,
                    "user": user_key,
                    "title": str(payload.get("title") or "Photobook Radar")[:250],
                    "message": (possible_repeat + str(payload.get("message") or ""))[:1024],
                    "url": str(payload.get("url") or ""),
                    "url_title": "Open listing",
                    "priority": 0,
                }
                with httpx.Client(timeout=15, follow_redirects=False) as client:
                    reply = client.post("https://api.pushover.net/1/messages.json", data=message)
                reply.raise_for_status()
                response = reply.json()
                if response.get("status") != 1:
                    raise RuntimeError("Push provider rejected the message")
            else:
                raise ValueError("Unconfigured notification channel")
        with transaction(db):
            changed = db.execute("UPDATE notification_events SET status='PROVIDER_ACCEPTED',provider_request=?,provider_receipt=?,accepted_at=?,lease_token=NULL,lease_until=NULL WHERE id=? AND lease_token=?", (response.get("request"), response.get("receipt"), now(), event["id"], event["lease_token"]))
            if changed.rowcount != 1:
                raise RuntimeError("Stale notification lease")
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        with transaction(db):
            retry_at = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat(timespec="seconds").replace("+00:00", "Z") if event["attempts"] < 2 else None
            db.execute("UPDATE notification_events SET status='DELIVERY_UNKNOWN',last_error=?,lease_token=NULL,lease_until=? WHERE id=? AND lease_token=?", (type(exc).__name__, retry_at, event["id"], event["lease_token"]))
    except httpx.HTTPStatusError as exc:
        with transaction(db):
            db.execute("UPDATE notification_events SET status='FAILED',last_error=?,lease_token=NULL,lease_until=NULL WHERE id=? AND lease_token=?", (f"Provider HTTP {exc.response.status_code}", event["id"], event["lease_token"]))
    except Exception as exc:
        with transaction(db):
            db.execute("UPDATE notification_events SET status='FAILED',last_error=?,lease_token=NULL,lease_until=NULL WHERE id=? AND lease_token=?", (str(exc)[:250], event["id"], event["lease_token"]))
    return True
