# topstep_ws.py
import logging
import threading
from datetime import datetime
import time
import requests
from zoneinfo import ZoneInfo
from collections import defaultdict

from signalrcore.hub_connection_builder import HubConnectionBuilder

from utils import round_half_up, round_to_tick, within_market_hours
from api import (
    place_trailing_stop,
    cancel_order,
    search_open_orders,
    get_open_positions,
    place_stop_loss_order,
    get_net_position_for_contract
)

# ── Constants ──────────────────────────────────────────────────────────────────
EST = ZoneInfo("America/New_York")

# ── Shared signalR quote hub (single connection) ───────────────────────────────
class QuoteBus:
    def __init__(self, token: str, contract_id: str):
        self.token = token
        self.contract_id = contract_id
        self.hub = None
        self.connected = False
        self.listeners = []
        self.last_tick_ms = 0

    def set_token(self, token: str):
        """Allow external refresher to update the token used on reconnects."""
        self.token = token

    def is_connected(self) -> bool:
        return bool(self.hub) and self.connected

    def wait_until_connected(self, timeout_s=6) -> bool:
        end = time.time() + timeout_s
        while time.time() < end:
            if self.is_connected():
                return True
            time.sleep(0.2)
        return False

    def start(self):
        # safe to call repeatedly
        if self.hub and self.connected:
            return
        if self.hub and not self.connected:
            try: self.hub.stop()
            except Exception: pass
            self.hub = None

        self.hub = (
            HubConnectionBuilder()
            .with_url(
                f"wss://rtc.topstepx.com/hubs/market?access_token={self.token}",
                options={"access_token_factory": lambda: self.token,
                         "skip_negotiation": True, "transport": "websockets"},
            )
            .configure_logging(logging.INFO)
            .build()
        )

        def on_open():
            self.connected = True
            logging.info("✅ QuoteBus connected; subscribing %s", self.contract_id)
            try:
                self.hub.send("SubscribeContractTrades", [self.contract_id])
            except Exception:
                logging.exception("Subscribe failed")
            self.last_tick_ms = int(time.time()*1000)

        def on_close():
            self.connected = False
            logging.info("🔌 QuoteBus disconnected for %s", self.contract_id)
            # self.hub left in place; guard thread will restart post-pause

        def on_error(err):
            self.connected = False
            logging.error("QuoteBus error: %r", err)

        def on_trade(args):
            # only updates listeners during allowed hours
            now = datetime.now(EST)
            if not ((now.hour < 16) or (now.hour >= 18)):
                return
            self.last_tick_ms = int(time.time()*1000)
            _, trades = args
            for t in trades:
                p = t.get("price"); ts = t.get("timestamp") or t.get("tradeTime")
                if p is None or ts is None: continue
                for cb in list(self.listeners):
                    try: cb(float(p), ts)
                    except Exception: logging.exception("Listener error")

        self.hub.on_open(on_open)
        try: self.hub.on_close(on_close)
        except Exception: pass
        try: self.hub.on_error(on_error)
        except Exception: pass
        self.hub.on("GatewayTrade", on_trade)
        self.hub.start()

    def stop(self):
        """Stop and mark disconnected."""
        try:
            if self.hub:
                self.hub.stop()
        finally:
            self.hub = None
            self.connected = False
            # keep listeners list; they'll be reused on reconnect

# ── Watcher registry so we can cancel on 'close' ───────────────────────────────
ACTIVE_WATCHERS = defaultdict(list)  # contract_id -> list of {bus, listener, done, tag}

def cancel_trailing_watchers(contract_id: str) -> int:
    """
    Cancel (detach) all trailing trigger watchers for a contract_id.
    Returns number canceled and logs it.
    """
    arr = ACTIVE_WATCHERS.get(contract_id, [])
    count = 0
    for w in arr:
        try:
            w["done"].set()
            try:
                w["bus"].listeners.remove(w["listener"])
            except ValueError:
                pass
            count += 1
        except Exception:
            logging.exception("Error canceling watcher for %s", contract_id)
    ACTIVE_WATCHERS[contract_id] = []
    logging.info("🧹 Canceled %d trailing watcher(s) for %s", count, contract_id)
    return count

# ── Per-trade trigger watcher ──────────────────────────────────────────────────
def watch_trigger_and_place_trailer(
    quote_bus: QuoteBus,
    token: str,
    account_id: int,
    contract_id: str,
    side: int,           # 0=buy, 1=sell (entry side)
    size: int,
    entry_order_id: int,
    trigger_price: float,
    atr_points: float,   # ticks→points
    tag: str,
    stop_loss_price: float | None = None,
    baseline_net: int = 0
):

    stop_side = 1 - side
    done = threading.Event()

    # NEW: track entry-fill via open-orders (no positions polling)
    entry_still_open = True
    last_orders_check_ms = 0
    orders_check_interval_ms = 500
    orders_backoff_until_ms = 0

    def _now_ms():
        return int(time.time() * 1000)

    static_stop_id = None  # broker stop id, once placed after fill
    trailing_order_id = None  # id after we place trailing stop

    logging.info(
        "[%s] 👀 Trigger watcher started: side=%s size=%s entry_order_id=%s trigger=%s atr_points=%s",
        tag, side, size, entry_order_id, trigger_price, atr_points
    )

    def _entry_open_now() -> bool:
        """Check if the entry order is still in open orders."""
        try:
            open_orders = search_open_orders(token, account_id)
            return any(
                (o.get("id") == entry_order_id or o.get("orderId") == entry_order_id) and int(o.get("type", -1)) == 1
                for o in open_orders
            )
        except requests.HTTPError as e:
            if getattr(e, "response", None) is not None and e.response.status_code == 429:
                return True  # rate-limited → assume still open until next tick
            logging.exception("[%s] searchOpen failed during _entry_open_now", tag)
            return True

    def _position_net() -> int:
        try:
            return get_net_position_for_contract(token, account_id, contract_id)
        except Exception:
            logging.exception("[%s] get_net_position_for_contract failed", tag)
            return baseline_net

    def _grace_confirm_fill(timeout_s=2.0, poll_ms=100) -> bool:
        """
        Return True if we confirm a fill within the window by either:
          • entry order disappears from open orders, OR
          • net position != baseline (indicates a fill landed).
        """
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            still_open = _entry_open_now()
            net_now = _position_net()
            if (not still_open) or (net_now != baseline_net and abs(net_now) >= 1):
                logging.info("[%s] ✅ Grace confirm: filled detected (open=%s, net=%s, baseline=%s)",
                             tag, still_open, net_now, baseline_net)
                return True
            time.sleep(poll_ms / 1000.0)
        return False

    def _listener(last_price: float, ts: str):
        nonlocal entry_still_open, last_orders_check_ms, orders_backoff_until_ms
        nonlocal static_stop_id, trailing_order_id
        if done.is_set():
            return

        # --- 0) Throttled check: did our entry leave open orders? (→ filled or canceled) ---
        now_ms = _now_ms()
        if now_ms >= orders_backoff_until_ms and (now_ms - last_orders_check_ms) >= orders_check_interval_ms:
            last_orders_check_ms = now_ms
            try:
                open_orders = search_open_orders(token, account_id)
                entry_still_open = any(
                    (o.get("id") == entry_order_id or o.get("orderId") == entry_order_id) and o.get("type") == 1
                    for o in open_orders
                )
            except requests.HTTPError as e:
                if getattr(e, "response", None) is not None and e.response.status_code == 429:
                    orders_backoff_until_ms = now_ms + 3000  # 3s backoff
                else:
                    logging.exception("[%s] searchOpen failed", tag)

        # If entry has filled (not found in open orders) and we have a stop level but no static stop yet → place it
        if not entry_still_open and stop_loss_price is not None and static_stop_id is None:
            try:
                resp = place_stop_loss_order(token, account_id, contract_id, stop_side, size, stop_loss_price)
                static_stop_id = resp.get("orderId") or resp.get("id")
                logging.info("[%s] 🧷 Static stop placed @ %s (id=%s)", tag, stop_loss_price, static_stop_id)
            except Exception:
                logging.exception("[%s] Failed to place static stop @ %s", tag, stop_loss_price)

        # --- 1) If stop level touched before trigger, kill trailer path and clean up ---
        if stop_loss_price is not None:
            stop_hit = (side == 0 and last_price <= stop_loss_price) or (side == 1 and last_price >= stop_loss_price)
            if stop_hit:
                logging.info("[%s] ⛔ Stop level touched %s (last=%s) → cancel trailer watcher.", tag, stop_loss_price,
                             last_price)
                # if entry still open, cancel it so it can't fill after the stop
                if entry_still_open:
                    try:
                        cancel_order(token, account_id, entry_order_id)
                        logging.info("[%s] Canceled still-open entry limit %s", tag, entry_order_id)
                    except Exception:
                        logging.exception("[%s] Failed to cancel entry %s", tag, entry_order_id)
                # if we somehow placed a trailing stop already, cancel it (keep static stop in charge)
                if trailing_order_id:
                    try:
                        cancel_order(token, account_id, trailing_order_id)
                        logging.info("[%s] Canceled trailing stop %s", tag, trailing_order_id)
                    except Exception:
                        logging.exception("[%s] Failed to cancel trailing %s", tag, trailing_order_id)
                done.set()
                return

        # --- 2) Trigger logic ---
        hit = (side == 0 and last_price >= trigger_price) or (side == 1 and last_price <= trigger_price)
        if not hit:
            return

        logging.info("[%s] 🔔 Trigger condition met: last=%s trigger=%s", tag, last_price, trigger_price)

        # ---- GRACE CONFIRM FILL BEFORE CANCEL ----
        if entry_still_open:
            if _grace_confirm_fill(timeout_s=2.0, poll_ms=100):
                entry_still_open_local = _entry_open_now()
                if not entry_still_open_local and stop_loss_price is not None and static_stop_id is None:
                    try:
                        resp = place_stop_loss_order(token, account_id, contract_id, stop_side, size, stop_loss_price)
                        static_stop_id = resp.get("orderId") or resp.get("id")
                        logging.info("[%s] 🧷 Static stop placed @ %s (id=%s)", tag, stop_loss_price, static_stop_id)
                    except Exception:
                        logging.exception("[%s] Failed to place static stop after grace", tag)
                # Fall through to trailing placement
            else:
                # Still looks unfilled after grace window → cancel and exit
                try:
                    cancel_order(token, account_id, entry_order_id)
                    logging.warning("[%s] Trigger hit but limit not filled (after grace) → canceled %s",
                                    tag, entry_order_id)
                except Exception:
                    logging.exception("[%s] Failed to cancel entry %s", tag, entry_order_id)
                done.set()
                return

        # Assume filled → place trailing stop
        trail_price = round_to_tick(last_price - atr_points + 0.25 if side == 0 else last_price + atr_points - 0.25)
        resp = place_trailing_stop(token, account_id, contract_id, stop_side, size, trail_price)
        trailing_order_id = resp.get("orderId") or resp.get("id")

        # Replace static stop with trailing
        if static_stop_id:
            try:
                cancel_order(token, account_id, static_stop_id)
                logging.info("[%s] Replaced static stop (id=%s) with trailing (id=%s @ %s)",
                             tag, static_stop_id, trailing_order_id, trail_price)
                static_stop_id = None
            except Exception:
                logging.exception("[%s] Failed to cancel static stop %s after trailing", tag, static_stop_id)

        logging.info("[%s] 🎯 Trigger HIT @ %s → trailing stop @ %s (id=%s, offset≈%s pts)",
                     tag, last_price, trail_price, trailing_order_id, atr_points)
        done.set()

    # register & attach listener
    ACTIVE_WATCHERS[contract_id].append({"bus": quote_bus, "listener": _listener, "done": done, "tag": tag})
    quote_bus.listeners.append(_listener)

    # keep alive until canceled or finished
    done.wait(timeout=60 * 60 * 8)  # safety auto-stop after 8h

    # detach & cleanup
    try:
        quote_bus.listeners.remove(_listener)
    except ValueError:
        pass
    try:
        lst = ACTIVE_WATCHERS.get(contract_id, [])
        ACTIVE_WATCHERS[contract_id] = [w for w in lst if w["listener"] is not _listener]
    except Exception:
        pass