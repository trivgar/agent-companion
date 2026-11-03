"""Notifications from the dashboard itself, under its own name.

Everything before this went out through somebody else's app, so Android labelled
it with theirs: "Termux:API", then "Home Assistant". The label is the app that
posted, and there is no field to override it -- the only way to carry the
dashboard's name is to be the app doing the posting.

The dashboard already is one. It ships a manifest and a service worker, and installs
to a phone's home screen; what it lacks is a secure origin, because Web Push
needs one and `http://myhost:8770` is not. `tailscale serve` fixes that with a
real Let's Encrypt certificate on the tailnet name.

WHAT THIS DOES NOT COVER, deliberately. The sender is the dashboard, so it can
report anything except its own death. Infrastructure alerts -- TTS down, Home
Assistant down -- stay on `phone-watch.sh`, which runs on a systemd timer
independent of this process and pushes through Home Assistant. Two senders, each
covering the blind spot of the other. That split matters: a notifier that cannot
report its own failure is how this morning's outage stayed invisible for
seventeen minutes.

Keys and subscriptions live in one folder, push_dir() (the push_dir setting,
else data_dir). A subscription that answers 404 or 410 is gone for good and is
dropped; anything else is the push service having a bad minute and is kept.
"""
from __future__ import annotations

import importlib
import json
import os
import time
from pathlib import Path

from agent_companion import config

VAPID_NAME = "session-companion-vapid.json"
SUBS_NAME = "session-companion-push.json"
# Contact address in the VAPID claim. Push services want a way to reach the
# sender about a misbehaving subscription; it is never shown to anyone.
VAPID_SUBJECT = str(config.get("contact", "mailto:admin@localhost"))


def push_dir() -> Path:
    """The folder of the VAPID key and the subscriptions, for every push this
    install sends or takes: the push_dir setting, else data_dir. A key minted
    anywhere else would leave every subscribed device signed by a key the push
    service no longer accepts."""
    raw = config.get("push_dir") or config.get("data_dir") or Path.home() / ".local" / "share" / "agent-companion"
    return Path(str(raw)).expanduser()


# Plaintext bytes a push can carry. FCM takes 4096 encrypted; aes128gcm adds
# about a hundred. Kept well under, so an embedded icon never costs a delivery.
PAYLOAD_MAX = 3600


def _b64(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _is_read_timeout(exc) -> bool:
    """Did the request leave the machine and the answer get lost?

    requests raises ReadTimeout once the body is sent and only the response is
    outstanding; ConnectTimeout and ConnectionError mean it never went. The
    distinction decides whether a fallback is a safety net or a duplicate.
    """
    name = type(exc).__name__
    if name in ("ConnectTimeout", "ConnectionError", "NewConnectionError"):
        return False
    if name == "ReadTimeout":
        return True
    # pywebpush wraps; look at the cause as well as the text, and only accept
    # "read timed out" rather than the word "timeout" on its own, which a
    # connect timeout also carries.
    text = f"{exc} {getattr(exc, '__cause__', '')}".lower()
    return "read timed out" in text or "readtimeout" in text


def _raw_private(key) -> bytes:
    """The 32-byte private scalar, which is the form push libraries want."""
    return key.private_numbers().private_value.to_bytes(32, "big")


def pick_delivery(payload: str) -> tuple[int, str]:
    """(TTL seconds, Urgency) for a push, by its tag: a spoken clip's
    subtitle (sub-<Name>) is stale in minutes and need not wake the phone;
    everything else (asks, needs-you, holds, alerts) waits up to a day and
    wakes it."""
    try:
        tag = str(json.loads(payload).get("tag") or "")
    except (ValueError, AttributeError):
        tag = ""
    if tag.startswith("sub-"):
        return 300, "normal"
    return 86400, "high"


def _icons():
    """The `icons` setting names a module with icon_data(tag, title) and
    icon_for(tag, title): an embedded picture and a URL one. None: no picture."""
    name = config.get("icons")
    if name:
        try:
            m = importlib.import_module(str(name))
            return m.icon_data, m.icon_for
        except Exception:
            pass
    return (lambda tag, title: ""), (lambda tag, title: "")


class PushStore:
    """VAPID keys and browser subscriptions, persisted."""

    def __init__(self, home) -> None:
        self.home = Path(home)

    # -- keys ------------------------------------------------------------
    def _keys(self) -> dict:
        path = self.home / VAPID_NAME
        try:
            d = json.loads(path.read_text())
            if isinstance(d, dict) and d.get("private") and d.get("public"):
                return d
        except (OSError, ValueError):
            pass

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        key = ec.generate_private_key(ec.SECP256R1())
        raw_pub = key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        # pywebpush wants the raw 32-byte private scalar as base64url, not a
        # PKCS8 PEM -- handed a PEM it fails with "Could not deserialize key
        # data", which send() then swallows, so every push silently delivers to
        # nobody. The PEM is kept only because it is the readable form.
        d = {"private": pem, "private_raw": _b64(_raw_private(key)),
             "public": _b64(raw_pub), "at": time.time()}
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.sc-tmp")
            tmp.write_text(json.dumps(d))
            # The private key signs every push this machine sends. Nobody else
            # on the box needs to read it.
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            pass
        return d

    def _log(self, msg: str) -> None:
        try:
            with (self.home / "tts" / "speak.log").open("a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} PUSH-FAIL {msg}\n")
        except OSError:
            pass

    def _save_keys(self, d: dict) -> None:
        path = self.home / VAPID_NAME
        try:
            tmp = path.with_suffix(".json.sc-tmp")
            tmp.write_text(json.dumps(d))
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except OSError:
            pass

    def public_key(self) -> str:
        """The applicationServerKey the browser subscribes with.

        Generated once and kept: a fresh key on every restart would leave every
        existing subscription signed by a key the push service no longer accepts,
        and they would fail silently.
        """
        return self._keys().get("public", "")

    # -- subscriptions ---------------------------------------------------
    def _path(self) -> Path:
        return self.home / SUBS_NAME

    def subscriptions(self) -> list[dict]:
        try:
            d = json.loads(self._path().read_text())
        except (OSError, ValueError):
            return []
        subs = d.get("subscriptions") if isinstance(d, dict) else None
        return [s for s in subs if isinstance(s, dict) and s.get("endpoint")] \
            if isinstance(subs, list) else []

    def _write(self, subs: list[dict]) -> None:
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            tmp = self._path().with_suffix(".json.sc-tmp")
            tmp.write_text(json.dumps({"subscriptions": subs, "at": time.time()}))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path())
        except OSError:
            pass

    def subscribe(self, sub, person: str | None = None) -> bool:
        """Add a device. Keyed on endpoint, so re-subscribing replaces rather
        than duplicates -- a browser re-registers on its own schedule.

        WHOSE DEVICE (2026-09-26): `person` is someone the owner has added; None
        is the owner, as every device subscribed before people existed was."""
        if not isinstance(sub, dict) or not sub.get("endpoint"):
            return False
        subs = [s for s in self.subscriptions() if s["endpoint"] != sub["endpoint"]]
        row = {"endpoint": sub["endpoint"], "keys": sub.get("keys") or {}}
        if person:
            row["person"] = person
        subs.append(row)
        self._write(subs)
        return True

    def devices(self, person: str | None = None) -> int:
        """How many devices a person has subscribed (None: the owner's)."""
        return sum(1 for s in self.subscriptions() if (s.get("person") or None) == person)

    # -- sending ---------------------------------------------------------
    # Called with (title, body, url, tag) for every push this sends, delivered
    # or not: the Push page's feed (app/alerts.py). Never lets a send fail.
    on_send = None

    def send(self, title: str, body: str, url: str = "", tag: str = "",
             sender=None, record: bool = True, to: str | None = None,
             replay: int | None = None, image: str = "", actions: list | None = None) -> int:
        """Push to one person's devices: the owner's (to=None) or someone
        the owner has added. Returns how many were delivered.
        Theirs are never recorded in the owner's Push feed.

        Never raises: this is called from hooks and from the request path, and a
        notification that cannot be sent must not take anything else down.
        """
        if record and to is None and self.on_send:
            try:
                self.on_send(title, body, url, tag)
            except Exception:
                pass
        everyone = self.subscriptions()
        subs = [s for s in everyone if (s.get("person") or None) == to]
        if not subs:
            return 0
        # ensure_ascii=False, so the wire carries real UTF-8 rather than
        # \u2014 escapes. Both are valid JSON and a correct decoder is
        # indifferent -- but the escapes did show up on a real phone, so
        # something in the chain is not decoding, and an escape that is never
        # emitted cannot leak. Web Push payloads are UTF-8 by definition.
        # The right-hand picture: who or what sent it, chosen from the tag
        # by the `icons` module, if one is set. Empty means none.
        # EMBEDDED when it fits: a URL has to be fetched
        # over the tailnet, which an idle phone has not brought back up in time.
        # Web Push carries about 4 KB once encrypted, so a long body falls back
        # to the URL, which still works whenever the phone is awake.
        icon_data, icon_for = _icons()

        def build(icon):
            d = {"title": title, "body": body, "url": url,
                 "tag": tag or "session-companion", "icon": icon}
            # A comms clip the phone fetched: sw.js adds "Play again".
            if replay is not None:
                d["replay"] = int(replay)
            # Something that needs you buzzes even when it replaces an older
            # one of the same tag (an agent's newer question or permission ask,
            # tagged needs-<Name> since 2026-09-29). Clips' notices stay quiet.
            if (tag or "").startswith("needs-") or tag == "print-watch":
                d["renotify"] = True
            # A picture in the notification (Bort's camera frame), and buttons
            # (sw.js): [{action, title}], each opening `url` with &act=<action>.
            if image:
                d["image"] = image
            if actions:
                d["actions"] = [{"action": str(a["action"])[:20], "title": str(a["title"])[:30]} for a in actions[:2]]
            return json.dumps(d, ensure_ascii=False)
        payload = build(icon_data(tag, title))
        if len(payload.encode("utf-8")) > PAYLOAD_MAX:
            payload = build(icon_for(tag, title))
        send = sender or self._default_sender()
        if send is None:
            return 0

        ok, keep = 0, []
        for sub in subs:
            try:
                send(sub, payload)
                ok += 1
                keep.append(sub)
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                # A READ timeout is not a failure to send. The request went out
                # and FCM's reply was lost, so the notification very probably
                # arrived -- treating it as failed made speak.sh fall back to
                # Home Assistant and the same clip arrived twice, once under
                # each app's name. A CONNECT failure is different: nothing left
                # the machine, and the fallback is exactly right for it.
                if _is_read_timeout(exc):
                    self._log(f"read timeout, assuming delivered: {str(exc)[:90]}")
                    ok += 1
                    keep.append(sub)
                    continue
                # Record it. A swallowed failure here returns 0, which reads
                # exactly like "nobody is subscribed" -- a malformed VAPID key
                # meant every push silently reached nobody and looked normal.
                self._log(f"send failed: {type(exc).__name__} "
                          f"status={status} {str(exc)[:120]}")
                # 404/410 mean the subscription is gone for good. Anything else
                # is the push service having a bad minute; dropping the device
                # for that would turn a blip into permanent silence.
                if status in (404, 410):
                    continue
                keep.append(sub)
        if len(keep) != len(subs):
            # Only the dead ones go: the list on disk is everyone's, not just
            # the devices this push was for.
            alive = {s["endpoint"] for s in keep}
            dead = {s["endpoint"] for s in subs} - alive
            self._write([s for s in everyone if s["endpoint"] not in dead])
        return ok

    def _default_sender(self):
        try:
            from pywebpush import webpush
        except ImportError:
            return None
        keys = self._keys()
        priv = keys.get("private_raw")
        if not priv:
            # A key file written before private_raw existed. Derive it from the
            # PEM and keep the SAME key: regenerating would silently orphan
            # every subscription already registered against the old public key.
            pem = keys.get("private")
            if not pem:
                return None
            try:
                from cryptography.hazmat.primitives import serialization
                k = serialization.load_pem_private_key(pem.encode(), password=None)
                priv = _b64(_raw_private(k))
                keys["private_raw"] = priv
                self._save_keys(keys)
            except Exception:
                return None

        def send(sub, payload):
            # 15s, not 8. The read that timed out at 8 had already delivered,
            # and every such timeout costs a duplicate notification under
            # another app's name. Waiting longer is cheaper than guessing.
            #
            # TTL and Urgency: pushes seemed to stop until the dashboard was
            # reopened. pywebpush's default
            # TTL is 0, which tells the push service "deliver right now or
            # drop it": a phone asleep in Android's Doze was never reachable
            # "right now", so every push while it slept was thrown away, and
            # they seemed to come back when the app was opened because the
            # phone was awake again. Now a push waits for the phone, and
            # Urgency: high lets it wake a dozing phone. A clip's subtitle is
            # only worth a few minutes and does not wake it.
            ttl, urgency = pick_delivery(payload)
            webpush(subscription_info=sub, data=payload,
                    vapid_private_key=priv,
                    vapid_claims={"sub": VAPID_SUBJECT},
                    timeout=15, ttl=ttl, headers={"Urgency": urgency})
        return send
