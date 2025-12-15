# server.py
import logging
import threading
import pytz
import re
import requests
import subprocess
import os
import time
from flask import Flask, request, jsonify
from datetime import datetime, timedelta
from datetime import datetime as _dt
from dotenv import load_dotenv
from logging_setup import setup_logging
from config import config
from utils import round_half_up, round_to_tick, within_market_hours, is_trading_paused, now_ms
from api import (
    authenticate_topstepx,
    get_account_id,
    get_contract_id,
    place_limit_order,
    place_market_order,
    cancel_open_orders_for_contract,
    get_net_position_for_contract,
    close_position_contract,
    search_open_orders,
    cancel_order,
    wait_until_flat,
    cancel_stop_markets_for_contract, cancel_trailing_stops_for_contract
)
from topstep_ws import (
    QuoteBus,
    watch_trigger_and_place_trailer,
    cancel_trailing_watchers,
)

# Load environment variables
load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
setup_logging()
app = Flask(__name__)


# ── ngrok Integration ──────────────────────────────────────────────────────────
def start_ngrok():
    """Start ngrok with static domain from .env file"""
    ngrok_domain = os.getenv("NGROK_DOMAIN")

    if not ngrok_domain:
        logging.warning("NGROK_DOMAIN not set in .env file - ngrok will use random URL")
        logging.info("Add 'NGROK_DOMAIN=your-domain.ngrok-free.dev' to .env for static URL")
        ngrok_cmd = ["ngrok.exe", "http", "5000"]
    else:
        logging.info(f"Starting ngrok with static domain: {ngrok_domain}")
        ngrok_cmd = ["ngrok.exe", "http", "5000", f"--domain={ngrok_domain}"]

    ngrok_path = os.path.join(os.path.dirname(__file__), "ngrok.exe")

    if not os.path.exists(ngrok_path):
        logging.error(f"ngrok.exe not found at: {ngrok_path}")
        return None

    try:
        # Start ngrok in a new console window (Windows)
        if os.name == 'nt':  # Windows
            process = subprocess.Popen(
                ngrok_cmd,
                cwd=os.path.dirname(__file__),
                creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        else:  # Mac/Linux
            process = subprocess.Popen(
                ngrok_cmd,
                cwd=os.path.dirname(__file__),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

        logging.info("ngrok process started")
        return process
    except Exception as e:
        logging.error(f"Failed to start ngrok: {e}")
        return None


def get_ngrok_url(max_retries=15, delay=1):
    """
    Fetch the public ngrok URL from the local ngrok API.

    Args:
        max_retries: Number of times to retry
        delay: Seconds to wait between retries

    Returns:
        ngrok public URL or None
    """
    for attempt in range(max_retries):
        try:
            response = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=2)
            if response.status_code == 200:
                data = response.json()
                tunnels = data.get("tunnels", [])

                for tunnel in tunnels:
                    if tunnel.get("proto") == "https":
                        public_url = tunnel.get("public_url")
                        return public_url

                # If no https tunnel, try http
                for tunnel in tunnels:
                    if tunnel.get("proto") == "http":
                        public_url = tunnel.get("public_url")
                        return public_url.replace("http://", "https://")

        except requests.exceptions.ConnectionError:
            if attempt < max_retries - 1:
                logging.info(f"Waiting for ngrok to start... (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                logging.error("Could not connect to ngrok API after all retries")

        except Exception as e:
            logging.error(f"Error fetching ngrok URL: {e}")

    return None


def display_ngrok_url():
    """Fetch and display the ngrok URL with instructions"""
    ngrok_url = get_ngrok_url()

    if ngrok_url:
        webhook_url = f"{ngrok_url}/webhook"

        print("\n" + "=" * 70)
        print("NGROK TUNNEL ACTIVE")
        print("=" * 70)
        print(f"Public URL: {ngrok_url}")
        print(f"Webhook URL: {webhook_url}")
        print("\n📋 COPY THIS URL TO TRADINGVIEW:")
        print(f"   {webhook_url}")
        print("\nngrok Dashboard: http://127.0.0.1:4040")
        print("=" * 70 + "\n")

        logging.info(f"✅ ngrok tunnel established: {ngrok_url}")
        return webhook_url
    else:
        print("\n" + "=" * 70)
        print("WARNING: Could not fetch ngrok URL")
        print("=" * 70)
        print("Please check:")
        print("  1. Is ngrok running?")
        print("  2. Visit: http://127.0.0.1:4040 to see the URL manually")
        print("=" * 70 + "\n")

        logging.warning("⚠Could not fetch ngrok URL automatically")
        return None

# ── Contract rolling logic ─────────────────────────────────────────────────────
_MONTH_CODE = {3:"H", 6:"M", 9:"U", 12:"Z"}

def _current_quarter_month(d: _dt) -> int:
    return (( (d.month - 1) // 3 ) + 1) * 3

def _third_friday(year:int, month:int) -> _dt:
    import calendar
    cal = calendar.monthcalendar(year, month)
    day = [wk[calendar.FRIDAY] for wk in cal if wk[calendar.FRIDAY] != 0][2]
    return _dt(year, month, day, 0, 0, 0)

def _roll_start(year:int, month:int) -> _dt:
    # start rolling ~8 trading days before 3rd Friday (approx 11 calendar days)
    return _third_friday(year, month) - timedelta(days=11)

def map_continuous_to_active_quarter(root: str = "NQ", now: _dt | None = None) -> str:
    now = now or _dt.utcnow()
    qm = _current_quarter_month(now)
    code = _MONTH_CODE[qm]
    yy = (now.year % 10)

    if now >= _roll_start(now.year, qm):
        # roll early into next quarter
        next_month = 3 if qm == 12 else (qm + 3)
        next_year  = now.year + 1 if qm == 12 else now.year
        code = _MONTH_CODE[next_month]
        yy = next_year % 10

    return f"{root}{code}{yy}"

def _quote_bus_guard():
    """Keeps QuoteBus stopped 4–6pm ET; otherwise ensures it's connected.
       Also restarts on any disconnect and pushes refreshed tokens."""
    while True:
        try:
            if is_trading_paused():
                if config.quote_bus and config.quote_bus.is_connected():
                    logging.info("⏸️ CME pause → stopping QuoteBus")
                    config.quote_bus.stop()
            else:
                if config.quote_bus is None:
                    config.quote_bus = QuoteBus(config.topstep_token, config.contract_id)
                config.quote_bus.set_token(config.topstep_token)
                if not config.quote_bus.is_connected():
                    logging.info("🔁 Guard: (re)starting QuoteBus for %s", config.contract_id)
                    config.quote_bus.start()
                # if connected but no ticks for 30s during hours, resubscribe by restart
                now_ms_val = now_ms()
                if config.quote_bus.is_connected() and (now_ms_val - config.quote_bus.last_tick_ms) > 30000:
                    logging.warning("🩺 No ticks in 30s → restarting QuoteBus")
                    config.quote_bus.stop()
                    config.quote_bus.start()
        except Exception:
            logging.exception("QuoteBus guard error")
        time.sleep(5)

def auth_refresher(interval_hours=2):
    while True:
        try:
            config.topstep_token = authenticate_topstepx()
            logging.info("🔄 Token refreshed.")
        except Exception:
            logging.exception("Auth refresh failed")
        time.sleep(interval_hours * 3600)

# ── TradingView parser ─────────────────────────────────────────────────────────
TV_RE = re.compile(
    r"""
    ^\s*Next\ Candle\ Predictor\s*:\s*order\s*
    (?P<direction>buy|sell)\s*@\s*
    (?P<size>\d+)\s*
    filled\ on\s*(?P<ticker>[^.]+)\.\s*
    Entry\ Price:\s*(?P<entry>[-+]?\d+(?:\.\d+)?)\s*
    Comment:\s*(?P<comment>.+?)\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

def parse_tv_alert(body_text: str):
    m = TV_RE.search(body_text or "")
    if not m:
        raise ValueError("Alert text did not match expected format.")

    direction = m.group("direction").lower()
    size      = int(m.group("size"))
    ticker    = m.group("ticker").strip()
    entry     = float(m.group("entry"))
    comment   = m.group("comment").strip()
    stop_loss = None
    msl = re.search(r"stop\s*loss\s*=\s*([-+]?\d+(?:\.\d+)?)", comment, flags=re.IGNORECASE)
    if msl:
        stop_loss = float(msl.group(1))

    atr = None
    m2 = re.search(r"atr\s*=\s*([0-9]+(?:\.[0-9]+)?)", comment, flags=re.IGNORECASE)
    if m2:
        atr = float(m2.group(1))

    # optional |ts= in comment (ms or s)
    ts_ms = None
    m3 = re.search(r"ts\s*=\s*(\d{10,13})", comment, flags=re.IGNORECASE)
    if m3:
        ts_ms = int(m3.group(1))
        if len(m3.group(1)) == 10:  # seconds → ms
            ts_ms *= 1000

    return {
        "direction": direction,
        "size": size,
        "ticker": ticker,
        "entryPrice": entry,
        "comment": comment,
        "atr": atr,
        "ts_ms": ts_ms,
        "stopLoss": stop_loss
    }

def resolve_contract_for_ticker(ticker_text: str) -> str:
    try_symbol = ticker_text.upper().strip()
    # Translate continuous/root to an active quarterly symbol before calling the API
    if try_symbol in ("NQ1!", "NQ", "NQMAIN"):
        mapped = map_continuous_to_active_quarter("NQ")
        logging.info("Mapped %s → %s (active)", try_symbol, mapped)
        try_symbol = mapped
    try:
        return get_contract_id(try_symbol)
    except Exception:
        logging.warning("Could not resolve ticker '%s'; falling back to %s", try_symbol, config.default_contract_symbol)
        return get_contract_id(config.default_contract_symbol)

# ── Post-close quarantine to kill stragglers ───────────────────────────────────
def post_close_quarantine(token, account_id, contract_id, duration_s=2.5, poll_ms=200):
    end = time.time() + duration_s
    total = 0
    while time.time() < end:
        try:
            orders = search_open_orders(token, account_id)
            for o in orders:
                cid = o.get("contractId") or o.get("contract", {}).get("id")
                if cid != contract_id:
                    continue
                oid = o.get("id") or o.get("orderId")
                if not oid:
                    continue
                try:
                    cancel_order(token, account_id, oid)
                    total += 1
                except Exception:
                    logging.exception("Quarantine cancel failed for order %s", oid)
        except Exception:
            logging.exception("Quarantine poll failed")
        time.sleep(poll_ms / 1000.0)
    logging.info("Post-close quarantine done for %s: canceled %s order(s) in %.1fs",
                 contract_id, total, duration_s)

# ── Webhook ────────────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():

    # hard block any trading during the pause window
    if is_trading_paused():
        # optional: include the raw body for debugging (keep it short in logs)
        logging.info("🛑 CME pause (16:00–18:00 ET) → ignoring webhook/trade request.")
        return jsonify({"status": "skipped", "reason": "cme_pause_window"}), 200

    """
    CLOSE:
      1) cancel trailing watchers
      2) cancel ALL open orders for this contract
      3) flatten via /Position/closeContract if net != 0
      4) short post-close quarantine to cancel stragglers
      + start holdoff window; optional ts gating if you include |ts=
    EXIT: ignored (trailers handle exits)
    ENTRY: limit at entry, trigger at entry ± rounded(ATR_ticks)*0.25, start watcher
    """
    # Accept either raw text body or {"message": "..."} JSON
    body_text = request.get_data(as_text=True) or ""
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            body_text = data["message"]

    try:
        parsed = parse_tv_alert(body_text)
    except Exception as e:
        logging.exception("Failed to parse TradingView alert")
        return jsonify({"error": f"Parse error: {e}"}), 400

    direction   = parsed["direction"]
    size        = parsed["size"]
    ticker      = parsed["ticker"]
    entry_price = parsed["entryPrice"]
    comment     = parsed["comment"]
    atr         = parsed["atr"]
    tv_ts_ms    = parsed.get("ts_ms")

    comment_lc = (comment or "").lower()

    try:
        contract_id = resolve_contract_for_ticker(ticker)
    except Exception as e:
        logging.exception("Contract lookup failed")
        return jsonify({"error": f"Contract lookup failed: {e}"}), 400

    lock = config.per_symbol_lock[contract_id]
    with lock:

        # === CLOSE path: cancel watchers → cancel orders → closeContract → quarantine ===
        if "close" in comment_lc:
            canceled_watchers = cancel_trailing_watchers(contract_id)

            # holdoff & optional ts capture
            config.close_holdoff_until_ms[contract_id] = now_ms() + config.close_holdoff_ms
            logging.info("🧯 Close holdoff: %s until %s (ms)", contract_id, config.close_holdoff_until_ms[contract_id])
            if parsed.get("ts_ms") is not None:
                config.last_close_ts_ms[contract_id] = parsed["ts_ms"]
                logging.info("🕰Recorded last close ts for %s: %s", contract_id, parsed["ts_ms"])

            # 1) cancel ALL open orders for this contract
            try:
                canceled_count, canceled_ids = cancel_open_orders_for_contract(config.topstep_token, config.account_id, contract_id)
                logging.info("Close cleanup: canceled %s open order(s) for %s | ids=%s",
                             canceled_count, contract_id, canceled_ids or [])
            except Exception:
                logging.exception("Cancel open orders failed on close for %s", contract_id)
                canceled_count, canceled_ids = -1, []

            # 2) ALWAYS ask broker to flatten whatever net exists
            close_position_contract(config.topstep_token, config.account_id, contract_id, tolerate_no_position=True)

            # 3) Verify flat; if not flat, try one more close + verify again
            flat, net = wait_until_flat(config.topstep_token, config.account_id, contract_id, timeout_s=3.0, poll_ms=200)
            if not flat:
                logging.warning("⚠Still not flat (net=%s) after first closeContract → retrying", net)
                close_position_contract(config.topstep_token, config.account_id, contract_id, tolerate_no_position=True)
                flat, net = wait_until_flat(config.topstep_token, config.account_id, contract_id, timeout_s=3.0, poll_ms=200)

            logging.info("Close result for %s → flat=%s net=%s", contract_id, flat, net)

            # 4) short post-close quarantine (async)
            threading.Thread(
                target=post_close_quarantine,
                args=(config.topstep_token, config.account_id, contract_id, 2.5, 200),
                daemon=True,
            ).start()

            return jsonify({
                "status": "close_done",
                "contractId": contract_id,
                "flat": bool(flat),
                "netAfter": int(net),
                "canceledWatchers": canceled_watchers,
                "canceledOrders": canceled_count,
                "canceledOrderIds": canceled_ids,
            }), 200

        # === EXIT path: ignore (your trailing logic handles it) ===
        if "exit" in comment_lc:
            logging.info("Skipping webhook: comment indicates exit → %s", comment)
            return jsonify({"status": "skipped", "reason": "exit signal"}), 200

        # === ENTRY path ===
        if atr is None:
            return jsonify({"error": "ATR not found; expected 'entry|atr=7' (ticks)"}), 400
        if direction not in ("buy", "sell"):
            return jsonify({"error": "direction must be 'buy' or 'sell'"}), 400
        if size <= 0:
            return jsonify({"error": "size must be > 0"}), 400
        if entry_price <= 0:
            return jsonify({"error": "entryPrice must be > 0"}), 400

        # FAILSAFE A (timestamp-aware): drop if entry.ts <= last_close.ts
        last_close_ts = config.last_close_ts_ms.get(contract_id)
        if tv_ts_ms is not None and last_close_ts is not None and tv_ts_ms <= last_close_ts:
            logging.info("⛔ Entry suppressed by ts for %s: entry.ts=%s ≤ last_close.ts=%s",
                         contract_id, tv_ts_ms, last_close_ts)
            return jsonify({
                "status": "skipped",
                "reason": "stale_vs_close_ts",
                "contractId": contract_id,
                "entryTs": tv_ts_ms,
                "lastCloseTs": last_close_ts
            }), 200

        # FAILSAFE B (time holdoff): drop entries during close holdoff window
        until = config.close_holdoff_until_ms.get(contract_id, 0)
        now_val = now_ms()
        if now_val < until:
            logging.info("⏱️ Entry suppressed by close holdoff for %s (now=%s < until=%s)",
                         contract_id, now_val, until)
            return jsonify({
                "status": "skipped",
                "reason": "in_close_holdoff",
                "contractId": contract_id,
                "now": now_val,
                "holdoffUntil": until
            }), 200

        # ATR ticks → rounded ticks (half-up) → points
        atr_ticks_raw = float(atr)
        atr_ticks_rounded = round_half_up(atr_ticks_raw)
        atr_points = atr_ticks_raw * config.tick_size

        side = 0 if direction == "buy" else 1
        entry_price = round_to_tick(entry_price if side == 0 else entry_price)
        trigger = round_to_tick(entry_price + atr_points if side == 0 else entry_price - atr_points)
        tag = f"{ticker}_{direction}_{datetime.utcnow():%Y%m%dT%H%M%S}"

        stop_loss_raw = parsed.get("stopLoss")
        stop_loss_px = round_to_tick(stop_loss_raw) if stop_loss_raw else None

        logging.info(
            "[%s] Parsed: dir=%s size=%s entry=%s atr_ticks(raw)=%s atr_ticks(rounded)=%s atr_points=%s "
            "trigger=%s stop_loss=%s comment=%s",
            tag, direction, size, entry_price, atr_ticks_raw, atr_ticks_rounded, atr_points, trigger,
            stop_loss_px, comment
        )

        # --- Flip-safety split: if webhook size==2 and this is a flip, market 1 first ---
        limit_size = size
        try:
            net = get_net_position_for_contract(config.topstep_token, config.account_id, contract_id)
            print(net)
        except Exception:
            net = 0

        if size == 8:
            if net == 0:
                logging.warning(
                    "[%s] ⚠️ REVERSAL BLOCKED: size=8 but net position is 0. "
                    "Treating as normal entry with size=4 instead.",
                    tag
                )
                size = 4
                limit_size = 4
            else:
                cancel_trailing_watchers(contract_id)

                try:
                    cnt, ids = cancel_trailing_stops_for_contract(config.topstep_token, config.account_id, contract_id)
                    logging.info("[%s] Reversal: canceled %d trailing stop(s): %s", tag, cnt, ids)
                except Exception:
                    logging.exception("[%s] Trailing stop cancel failed", tag)

                try:
                    cnt, ids = cancel_stop_markets_for_contract(config.topstep_token, config.account_id, contract_id)
                    logging.info("[%s] Reversal: canceled %d Stop Market(s): %s", tag, cnt, ids)
                except Exception:
                    logging.exception("[%s] Reversal stop-cancel failed", tag)

                logging.info(
                    "[%s] Flip-safety: size==14 and net=%s → MARKET %s x1 now, remainder as LIMIT.",
                    tag, net, "SELL" if side == 1 else "BUY"
                )
                try:
                    place_market_order(contract_id, side, 4)
                    limit_size = 4  # place only the remainder as limit
                except Exception:
                    logging.exception("[%s] Market leg of flip-safety failed; continuing with limit-only.", tag)
                    limit_size = 4  # fall back: place full limit if market leg failed

        # Place LIMIT entry (if any remainder)
        entry_order_id = None
        if limit_size > 0:
            try:
                ent = place_limit_order(contract_id, side=side, size=limit_size, price=entry_price)
                entry_order_id = ent.get("orderId") or ent.get("id")
                if entry_order_id is None:
                    raise RuntimeError(f"No orderId in response: {ent}")
            except requests.exceptions.ReadTimeout as e:
                # Our api.py already tried to reconcile. If we got here, no matching order was found.
                logging.error("Order submit timed out and could not be reconciled.")
                return jsonify({"error": "timeout_unconfirmed", "detail": str(e)}), 504
            except Exception as e:
                logging.exception("Failed to place limit order")
                return jsonify({"error": f"Failed to place limit order: {e}"}), 500

        # Start trigger watcher (places trailing stop when hit)
        try:
            quote_bus = ensure_quote_bus(contract_id)
            threading.Thread(
                target=watch_trigger_and_place_trailer,
                args=(quote_bus, config.topstep_token, config.account_id, contract_id,
                      side, limit_size, entry_order_id, trigger, atr_points, tag, stop_loss_px,
                      net),
                daemon=True,
            ).start()
            logging.info(
                "[%s] Trigger watcher scheduled: trigger=%s atr_points=%s side=%s size=%s",
                tag, trigger, atr_points, side, size
            )
        except Exception as e:
            logging.exception("Failed to start trigger watcher")
            return jsonify({"error": f"Failed to start trigger watcher: {e}"}), 500

        return jsonify({
            "status": "ok",
            "tag": tag,
            "contractId": contract_id,
            "limitOrderId": entry_order_id,
            "entryPrice": entry_price,
            "trigger": trigger,
            "atr": {
                "ticksRaw": atr_ticks_raw,
                "ticksRounded": atr_ticks_rounded,
                "points": atr_points
            },
            "parsed": {
                "direction": direction,
                "size": size,
                "ticker": ticker,
                "comment": comment,
                "ts_ms": tv_ts_ms
            }
        }), 200

def ensure_quote_bus(contract_id: str) -> 'QuoteBus':
    if config.quote_bus is None or config.quote_bus.contract_id != contract_id:
        config.quote_bus = QuoteBus(config.topstep_token, contract_id)
        config.quote_bus.start()
    return config.quote_bus

# ── Bootstrapping ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.info("Starting Trading Server…")

    # Start ngrok first
    logging.info("Initializing ngrok tunnel...")
    ngrok_process = start_ngrok()

    # Authenticate and setup
    config.topstep_token = authenticate_topstepx()
    config.account_id = get_account_id()

    # Prime default contract + quotes
    config.contract_id = get_contract_id(config.default_contract_symbol)
    config.quote_bus = QuoteBus(config.topstep_token, config.contract_id)
    config.quote_bus.start()

    # background token refresher
    threading.Thread(target=auth_refresher, args=(2,), daemon=True).start()

    # background guard to stop @4pm and auto-reconnect @6pm
    threading.Thread(target=_quote_bus_guard, daemon=True).start()

    # Display ngrok URL after a short delay for ngrok to start
    def show_ngrok_url():
        time.sleep(3)  # Give ngrok time to start
        display_ngrok_url()

    threading.Thread(target=show_ngrok_url, daemon=True).start()

    # Run Flask server
    logging.info("Starting Flask server on port 5000...")
    app.run(port=config.flask_port, debug=config.flask_debug, host=config.flask_host)