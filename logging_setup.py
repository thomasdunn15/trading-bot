# logging_setup.py
import logging, sys

def setup_logging(log_filename="trading.log"):
    root = logging.getLogger()
    if getattr(root, "_is_setup", False):
        return
    root._is_setup = True
    root.setLevel(logging.INFO)

    # console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root.addHandler(ch)

    # file handler with expanded filter
    class TradeFilter(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            return (
                msg.startswith("📩 Webhook received")
                or msg.startswith("📥 Limit order placed")
                or msg.startswith("⏱")
                or msg.startswith("🔁")
                or msg.startswith("❌")
                or msg.startswith("▶")
                or "📥 Entry@" in msg
                or "🎯 Trigger hit" in msg
                or "never filled" in msg
            )

    fh = logging.FileHandler(log_filename, mode="a", encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.addFilter(TradeFilter())
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    root.addHandler(fh)