# utils.py
"""Shared utility functions for trading bot"""
import time
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime
import pytz

# Constants
EST = pytz.timezone("America/New_York")
TICK_SIZE = 0.25  # NQ tick size


def round_half_up(x: float) -> int:
    """
    Round to nearest integer using half-up rounding.

    Examples:
        7.25 → 7
        7.5 → 8
        7.75 → 8

    Args:
        x: Number to round

    Returns:
        Rounded integer
    """
    return int(Decimal(str(x)).to_integral_value(rounding=ROUND_HALF_UP))


def round_to_tick(price: float, tick_size: float = TICK_SIZE) -> float:
    """
    Round price to nearest valid tick.

    Args:
        price: Price to round
        tick_size: Size of one tick (default: 0.25 for NQ)

    Returns:
        Price rounded to nearest tick
    """
    ticks = round(price / tick_size)
    return float(f"{ticks * tick_size:.2f}")


def within_market_hours(now: datetime = None) -> bool:
    """
    Check if currently within CME equity trading hours.

    CME equity futures trade nearly 24/5 with a daily pause from 4-6pm ET.

    Args:
        now: Current time (default: now in ET)

    Returns:
        True if markets are open (not in 4-6pm pause)
    """
    now = now or datetime.now(EST)
    # Not paused if before 4pm or after 6pm
    return now.hour < 16 or now.hour >= 18


def is_trading_paused(now: datetime = None) -> bool:
    """
    Returns True during the CME equity daily pause (16:00–18:00 ET).

    Args:
        now: Current time (default: now in ET)

    Returns:
        True if in daily trading pause
    """
    now = now or datetime.now(EST)
    return 16 <= now.hour < 18


def now_ms() -> int:
    """
    Get current time in milliseconds since epoch.

    Returns:
        Current timestamp in milliseconds
    """
    return int(time.time() * 1000)