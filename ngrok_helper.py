# ngrok_helper.py
import requests
import logging
import time


def get_ngrok_url(max_retries=10, delay=1):
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
                logging.info(f"⏳ Waiting for ngrok to start... (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                logging.error("❌ Could not connect to ngrok API. Is ngrok running?")

        except Exception as e:
            logging.error(f"❌ Error fetching ngrok URL: {e}")

    return None


def display_ngrok_url():
    """Fetch and display the ngrok URL with instructions"""
    ngrok_url = get_ngrok_url()

    if ngrok_url:
        webhook_url = f"{ngrok_url}/webhook"

        print("\n" + "=" * 70)
        print("🌐 NGROK TUNNEL ACTIVE")
        print("=" * 70)
        print(f"📍 Public URL: {ngrok_url}")
        print(f"🪝 Webhook URL: {webhook_url}")
        print("\n📋 COPY THIS URL TO TRADINGVIEW:")
        print(f"   {webhook_url}")
        print("\n🔗 ngrok Dashboard: http://127.0.0.1:4040")
        print("=" * 70 + "\n")

        logging.info(f"✅ ngrok tunnel established: {ngrok_url}")
        return webhook_url
    else:
        print("\n" + "=" * 70)
        print("⚠️  WARNING: Could not fetch ngrok URL")
        print("=" * 70)
        print("Please check:")
        print("  1. Is ngrok running?")
        print("  2. Visit: http://127.0.0.1:4040 to see the URL manually")
        print("=" * 70 + "\n")

        logging.warning("⚠️ Could not fetch ngrok URL automatically")
        return None