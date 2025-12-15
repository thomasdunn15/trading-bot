# api.py
"""TopstepX API client"""
import requests
import logging
import time
import urllib3.util.connection as urllib3_cn
import socket
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Optional, Dict, List, Tuple
from config import config


# Force all requests (urllib3) to IPv4 sockets only
def _force_ipv4():
    """Force urllib3 to use IPv4 only"""

    def allowed_gai_family():
        return socket.AF_INET  # IPv4 only

    urllib3_cn.allowed_gai_family = allowed_gai_family


_force_ipv4()


# Shared resilient HTTP session
def _make_session() -> requests.Session:
    """Create a requests session with retry logic and connection pooling"""
    s = requests.Session()
    retry = Retry(
        total=config.http_retry_total,
        connect=config.http_retry_connect,
        read=config.http_retry_read,
        backoff_factor=config.http_backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PUT", "DELETE", "PATCH")
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=20,
        pool_block=False
    )
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = _make_session()

# Cache for contract ID lookups
CONTRACT_ID_MAP: Dict[str, str] = {}


def _auth_header(token: Optional[str] = None) -> Dict[str, str]:
    """Generate authorization header"""
    token = token or config.topstep_token
    return {"Authorization": f"Bearer {token}"}


# ── Authentication ─────────────────────────────────────────────────────────────

def authenticate_topstepx() -> str:
    """
    Login with API key and return authentication token.

    Returns:
        Authentication token string

    Raises:
        requests.HTTPError: If authentication fails
    """
    r = SESSION.post(
        f"{config.topstep_api_base}/api/Auth/loginKey",
        json={
            "userName": config.topstep_username,
            "apiKey": config.topstep_api_key
        },
        headers={"Accept": "text/plain", "Content-Type": "application/json"},
        timeout=config.http_timeout,
    )
    r.raise_for_status()
    token = r.json()["token"]
    config.topstep_token = token
    logging.info("✅ Authenticated with TopstepX")
    return token


# ── Account Management ─────────────────────────────────────────────────────────

def get_account_id(token: Optional[str] = None) -> int:
    """
    Get first active account ID and cache it.

    Args:
        token: Optional authentication token (uses config if not provided)

    Returns:
        Account ID integer

    Raises:
        RuntimeError: If no active accounts found
        requests.HTTPError: If API call fails
    """
    if config.account_id:
        return config.account_id

    token = token or config.topstep_token
    r = SESSION.post(
        f"{config.topstep_api_base}/api/Account/search",
        json={"onlyActiveAccounts": True},
        headers=_auth_header(token),
        timeout=config.http_timeout,
    )
    r.raise_for_status()

    accounts = r.json().get("accounts", [])
    if not accounts:
        raise RuntimeError("❌ No active accounts found.")

    acct = accounts[2]
    config.account_id = acct["id"]
    logging.info(f"✅ Account: {acct['name']} (ID={config.account_id})")
    return config.account_id


# ── Contract Management ────────────────────────────────────────────────────────

def get_contract_id(symbol: str, live: bool = False) -> str:
    """
    Resolve Topstep contract ID by symbol with caching.

    Args:
        symbol: Contract symbol (e.g., 'NQU5')
        live: Whether to search live contracts only

    Returns:
        Contract ID string

    Raises:
        ValueError: If no matching contract found
        requests.HTTPError: If API call fails
    """
    symbol = symbol.upper().strip()

    # Check cache first
    if symbol in CONTRACT_ID_MAP:
        return CONTRACT_ID_MAP[symbol]

    r = SESSION.post(
        f"{config.topstep_api_base}/api/Contract/search",
        json={"searchText": symbol, "live": live},
        headers=_auth_header(),
        timeout=config.http_timeout,
    )
    r.raise_for_status()

    for c in r.json().get("contracts", []):
        if c.get("name", "").upper().startswith(symbol):
            contract_id = c["id"]
            CONTRACT_ID_MAP[symbol] = contract_id
            logging.info(f"✅ Contract {c['name']} → ID={contract_id}")
            return contract_id

    raise ValueError(f"❌ No matching contract for symbol: {symbol}")


# ── Order Placement ────────────────────────────────────────────────────────────

def place_limit_order(
        contract_id: str,
        side: int,
        size: int,
        price: float
) -> Dict:
    """
    Place a limit order (type=1).

    Args:
        contract_id: Contract ID to trade
        side: 0=Buy, 1=Sell
        size: Number of contracts
        price: Limit price

    Returns:
        Order response dict with orderId

    Raises:
        requests.exceptions.ReadTimeout: If request times out (will attempt reconciliation)
        requests.HTTPError: If API call fails
    """

    payload = {
        "accountId": get_account_id(),
        "contractId": contract_id,
        "type": 1,  # Limit
        "side": side,
        "size": size,
        "limitPrice": price,
    }
    url = f"{config.topstep_api_base}/api/Order/place"

    try:
        r = SESSION.post(
            url,
            json=payload,
            headers=_auth_header(),
            timeout=config.http_timeout
        )
        r.raise_for_status()
        order = r.json()
        logging.info(f"📥 Limit placed: {order}")
        return order

    except requests.exceptions.ReadTimeout:  # ✅ FIXED
        logging.error("⏱️ Order POST timed out; reconciling with broker…")
        # Try to confirm whether the order actually landed
        recon = _reconcile_limit_order(
            config.topstep_token,
            get_account_id(),
            contract_id,
            side,
            size,
            float(price)
        )
        if recon:
            return recon
        raise  # If not found, re-raise the timeout

    except Exception:
        logging.exception("place_limit_order failed")
        raise


def place_market_order(contract_id: str, side: int, size: int) -> Dict:
    """
    Place a market order (type=2).

    Args:
        contract_id: Contract ID to trade
        side: 0=Buy, 1=Sell
        size: Number of contracts

    Returns:
        Order response dict

    Raises:
        requests.HTTPError: If API call fails
    """
    r = SESSION.post(
        f"{config.topstep_api_base}/api/Order/place",
        json={
            "accountId": get_account_id(),
            "contractId": contract_id,
            "type": 2,  # Market
            "side": side,
            "size": size,
        },
        headers=_auth_header(),
        timeout=config.http_timeout,
    )
    r.raise_for_status()
    order = r.json()
    logging.info(f"📥 Market placed: {order}")
    return order


def place_trailing_stop(
        token: str,
        account_id: int,
        contract_id: str,
        side: int,
        size: int,
        trail_price: float
) -> Dict:
    """
    Place a trailing stop order (type=5).

    Args:
        token: Authentication token
        account_id: Account ID
        contract_id: Contract ID to trade
        side: 0=Buy, 1=Sell (exit side)
        size: Number of contracts
        trail_price: Trail price offset

    Returns:
        Order response dict

    Raises:
        requests.HTTPError: If API call fails
    """
    r = SESSION.post(
        f"{config.topstep_api_base}/api/Order/place",
        json={
            "accountId": account_id,
            "contractId": contract_id,
            "type": 5,  # Trailing Stop
            "side": side,
            "size": size,
            "trailPrice": trail_price,
        },
        headers=_auth_header(token),
        timeout=config.http_timeout,
    )
    r.raise_for_status()
    order = r.json()
    logging.info(f"🛑 Trailing stop placed: {order}")
    return order


def place_stop_loss_order(
        token: str,
        account_id: int,
        contract_id: str,
        side: int,
        size: int,
        stop_price: float
) -> Dict:
    """
    Place a stop market order (type=4).

    Args:
        token: Authentication token
        account_id: Account ID
        contract_id: Contract ID to trade
        side: 0=Buy, 1=Sell (exit side, opposite of entry)
        size: Number of contracts
        stop_price: Stop trigger price

    Returns:
        Order response dict

    Raises:
        requests.HTTPError: If API call fails
    """
    r = SESSION.post(
        f"{config.topstep_api_base}/api/Order/place",
        json={
            "accountId": account_id,
            "contractId": contract_id,
            "type": 4,  # Stop Market
            "side": side,
            "size": size,
            "stopPrice": stop_price
        },
        headers=_auth_header(token),
        timeout=config.http_timeout,
    )
    r.raise_for_status()
    order = r.json()
    logging.info(f"🛑 Static stop-loss placed: {order}")
    return order


# ── Order Management ───────────────────────────────────────────────────────────

def search_open_orders(token: str, account_id: int) -> List[Dict]:
    """
    Search for all open orders on account.

    Args:
        token: Authentication token
        account_id: Account ID

    Returns:
        List of order dicts

    Raises:
        requests.HTTPError: If API call fails
    """
    r = SESSION.post(
        f"{config.topstep_api_base}/api/Order/searchOpen",
        json={"accountId": account_id},
        headers=_auth_header(token),
        timeout=config.http_timeout,
    )
    r.raise_for_status()
    return r.json().get("orders", [])


def cancel_order(token: str, account_id: int, order_id: str):
    """
    Cancel a specific order.

    Args:
        token: Authentication token
        account_id: Account ID
        order_id: Order ID to cancel

    Raises:
        requests.HTTPError: If API call fails
    """
    r = SESSION.post(
        f"{config.topstep_api_base}/api/Order/cancel",
        json={"accountId": account_id, "orderId": order_id},
        headers=_auth_header(token),
        timeout=config.http_timeout,
    )
    r.raise_for_status()
    logging.info(f"❌ Order canceled: {order_id}")


def cancel_open_orders_for_contract(
        token: str,
        account_id: int,
        contract_id: str
) -> Tuple[int, List[str]]:
    """
    Cancel all open orders for a given contract.

    Args:
        token: Authentication token
        account_id: Account ID
        contract_id: Contract ID to cancel orders for

    Returns:
        Tuple of (count of canceled orders, list of canceled order IDs)
    """
    orders = search_open_orders(token, account_id)
    canceled_ids = []

    for o in orders:
        cid = o.get("contractId") or o.get("contract", {}).get("id")
        if cid != contract_id:
            continue

        oid = o.get("id") or o.get("orderId")
        if not oid:
            continue

        try:
            cancel_order(token, account_id, oid)
            canceled_ids.append(oid)
        except Exception:
            logging.exception("Cancel failed for order %s", oid)

    return len(canceled_ids), canceled_ids


def cancel_trailing_stops_for_contract(token: str, account_id: int, contract_id: str) -> tuple:
    """
    Cancel all trailing stop orders for a specific contract.

    Returns:
        Tuple of (count_canceled, list_of_order_ids)
    """
    orders = search_open_orders(token, account_id)
    canceled_ids = []

    for o in orders:
        cid = o.get("contractId") or o.get("contract", {}).get("id")
        if cid != contract_id:
            continue

        # Check if it's a trailing stop order (adjust field name based on your API)
        order_type = o.get("type") or o.get("orderType")
        if order_type in ("TrailingStop", "TRAILING_STOP", 5):  # Adjust based on your API's type values
            oid = o.get("id") or o.get("orderId")
            if oid:
                try:
                    cancel_order(token, account_id, oid)
                    canceled_ids.append(oid)
                except Exception:
                    logging.exception("Failed to cancel trailing stop order %s", oid)

    logging.info("🧹 Canceled %d trailing stop order(s) for %s: %s", len(canceled_ids), contract_id, canceled_ids)
    return len(canceled_ids), canceled_ids

def cancel_stop_markets_for_contract(
        token: str,
        account_id: int,
        contract_id: str
) -> Tuple[int, List[str]]:
    """
    Cancel ONLY Stop Market orders (type=4) for a contract.

    Args:
        token: Authentication token
        account_id: Account ID
        contract_id: Contract ID to cancel stops for

    Returns:
        Tuple of (count of canceled orders, list of canceled order IDs)
    """
    orders = search_open_orders(token, account_id)
    canceled_ids = []

    for o in orders:
        cid = o.get("contractId") or o.get("contract", {}).get("id")
        if str(cid) != str(contract_id):
            continue

        if o.get("type") != 4:  # Only Stop Market
            continue

        oid = o.get("id") or o.get("orderId")
        if not oid:
            continue

        try:
            cancel_order(token, account_id, oid)
            canceled_ids.append(oid)
        except Exception:
            logging.exception("Cancel STOP-MARKET failed for order %s", oid)

    logging.info(
        "🧹 Canceled %d Stop Market order(s) for %s: %s",
        len(canceled_ids), contract_id, canceled_ids
    )
    return len(canceled_ids), canceled_ids


# ── Position Management ────────────────────────────────────────────────────────

def get_open_positions(token: str, account_id: int) -> List[Dict]:
    """
    Get all open positions.

    Args:
        token: Authentication token
        account_id: Account ID

    Returns:
        List of position dicts

    Raises:
        requests.HTTPError: If API call fails
    """
    r = SESSION.post(
        f"{config.topstep_api_base}/api/Position/searchOpen",
        json={"accountId": account_id},
        headers=_auth_header(token),
        timeout=config.http_timeout,
    )
    r.raise_for_status()
    payload = r.json()
    print(payload)
    positions = payload.get("positions", payload if isinstance(payload, list) else [])
    return positions if isinstance(positions, list) else []


def _extract_net_qty(pos: Dict) -> int:
    """
    Extract net quantity from position dict (handles different API response formats).

    Args:
        pos: Position dict from API

    Returns:
        Net quantity (positive for long, negative for short)
    """
    # Try various field names
    for k in ("net", "netQty", "size", "quantityNet", "qtyNet"):
        if k in pos and isinstance(pos[k], (int, float)):
            return int(pos[k])

    # Fallback: calculate from long/short
    longq = int(pos.get("longQty", 0) or 0)
    shortq = int(pos.get("shortQty", 0) or 0)
    return longq - shortq


def get_net_position_for_contract(
        token: str,
        account_id: int,
        contract_id: str
) -> int:
    """
    Get net position quantity for a specific contract.

    Args:
        token: Authentication token
        account_id: Account ID
        contract_id: Contract ID to check

    Returns:
        Net position quantity (0 if no position)
    """
    for p in get_open_positions(token, account_id):
        cid = p.get("contractId") or p.get("contract", {}).get("id")
        if cid == contract_id:
            return _extract_net_qty(p)
    return 0


def close_position_contract(
        token: str,
        account_id: int,
        contract_id: str,
        tolerate_no_position: bool = True
) -> Dict:
    """
    Broker-side position flatten using closeContract endpoint.

    Args:
        token: Authentication token
        account_id: Account ID
        contract_id: Contract ID to close
        tolerate_no_position: If True, don't raise error if no position exists

    Returns:
        API response dict (empty if no position and tolerated)

    Raises:
        requests.HTTPError: If API call fails and not tolerated
    """
    try:
        r = SESSION.post(
            f"{config.topstep_api_base}/api/Position/closeContract",
            json={"accountId": account_id, "contractId": contract_id},
            headers=_auth_header(token),
            timeout=config.http_timeout,
        )

        if r.status_code >= 400:
            # Some servers 400/404 when already flat
            if tolerate_no_position:
                logging.info(
                    "ℹ️ closeContract returned %s but tolerated (likely already flat).",
                    r.status_code
                )
                return {}
            r.raise_for_status()
        else:
            logging.info("✅ closeContract accepted for %s", contract_id)

        return r.json() if r.content else {}

    except requests.HTTPError:  # ✅ FIXED
        if tolerate_no_position:
            logging.info("ℹ️ closeContract HTTP error tolerated (already flat?)")
            return {}
        raise


def wait_until_flat(
        token: str,
        account_id: int,
        contract_id: str,
        timeout_s: float = 3.0,
        poll_ms: int = 200
) -> Tuple[bool, int]:
    """
    Poll position until net == 0 or timeout.

    Args:
        token: Authentication token
        account_id: Account ID
        contract_id: Contract ID to monitor
        timeout_s: Timeout in seconds
        poll_ms: Polling interval in milliseconds

    Returns:
        Tuple of (is_flat: bool, last_net: int)
    """
    deadline = time.time() + timeout_s
    last_net = None

    while time.time() < deadline:
        try:
            last_net = get_net_position_for_contract(token, account_id, contract_id)
        except Exception:
            logging.exception("wait_until_flat: position check failed")
            last_net = None

        if last_net == 0:
            return True, 0

        time.sleep(poll_ms / 1000.0)

    return False, (last_net or 0)


# ── Helper Functions ───────────────────────────────────────────────────────────

def _orders_equal_price(a: float, b: float, tick: float = 0.25) -> bool:
    """
    Check if two prices are equal within half-tick tolerance.

    Args:
        a: First price
        b: Second price
        tick: Tick size (default 0.25)

    Returns:
        True if prices are within half-tick of each other
    """
    return abs(a - b) <= (tick / 2.0)


def _reconcile_limit_order(
        token: str,
        account_id: int,
        contract_id: str,
        side: int,
        size: int,
        price: float
) -> Optional[Dict]:
    """
    If a submit timed out, look for an open LIMIT order that matches our intent.

    Args:
        token: Authentication token
        account_id: Account ID
        contract_id: Contract ID
        side: Order side (0=Buy, 1=Sell)
        size: Order size
        price: Limit price

    Returns:
        Order dict if found, None otherwise
    """
    try:
        orders = search_open_orders(token, account_id)
    except Exception:
        logging.exception("Reconcile: search_open_orders failed")
        return None

    for o in orders:
        # Check contract
        if (o.get("contractId") or o.get("contract", {}).get("id")) != contract_id:
            continue

        # Check type (must be Limit)
        if int(o.get("type")) != 1:
            continue

        # Check side
        if int(o.get("side")) != int(side):
            continue

        # Check size
        if int(o.get("size")) != int(size):
            continue

        # Check price (within tolerance)
        lp = o.get("limitPrice") or o.get("price") or 0.0
        try:
            lp = float(lp)
        except Exception:
            continue

        if _orders_equal_price(lp, float(price)):
            logging.info("🔎 Reconcile: matched live LIMIT order after timeout → %s", o)
            return o

    return None