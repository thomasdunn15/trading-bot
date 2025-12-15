# config.py
"""Centralized configuration for trading bot"""
import os
import threading
import pytz
from collections import defaultdict
from typing import Dict, Optional
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class TradingConfig:
    """Centralized configuration management for trading bot"""

    def __init__(self):
        # API Configuration
        self.topstep_api_base = "https://api.topstepx.com"
        self.topstep_username = os.getenv("TOPSTEP_USERNAME")
        self.topstep_api_key = os.getenv("TOPSTEP_API_KEY")

        # Trading Configuration
        self.default_contract_symbol = "MNQZ5"
        self.tick_size = 0.25  # NQ tick size

        # Timing Configuration
        self.close_holdoff_ms = 1500  # Milliseconds to wait after close before accepting new entries
        self.auth_refresh_hours = 2  # Hours between token refreshes

        # Timezone
        self.timezone = pytz.timezone("America/New_York")

        # Runtime State (set during initialization)
        self.topstep_token: Optional[str] = None
        self.account_id: Optional[int] = None
        self.contract_id: Optional[str] = None
        self.quote_bus = None

        # Per-symbol state management
        self.per_symbol_lock: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self.close_holdoff_until_ms: Dict[str, int] = {}
        self.last_close_ts_ms: Dict[str, int] = {}

        # HTTP Configuration
        self.http_timeout = (5, 30)  # (connect, read) seconds
        self.http_retry_total = 5
        self.http_retry_connect = 3
        self.http_retry_read = 3
        self.http_backoff_factor = 0.4

        # Server Configuration
        self.flask_host = "0.0.0.0"
        self.flask_port = 5000
        self.flask_debug = False

    def validate(self):
        """Validate that all required configuration is present"""
        if not self.topstep_username:
            raise ValueError("TOPSTEP_USERNAME not configured")
        if not self.topstep_api_key:
            raise ValueError("TOPSTEP_API_KEY not configured")

    def __repr__(self):
        return (
            f"TradingConfig("
            f"contract={self.default_contract_symbol}, "
            f"tick_size={self.tick_size}, "
            f"account_id={self.account_id})"
        )


# Global config instance
config = TradingConfig()