# start_ngrok_static.py
import subprocess
import os
from dotenv import load_dotenv

load_dotenv()

def main():
    ngrok_domain = os.getenv("NGROK_DOMAIN")

    if not ngrok_domain:
        print("❌ ERROR: NGROK_DOMAIN not set in .env file")
        print("Add this line to your .env file:")
        print("NGROK_DOMAIN=your-domain.ngrok-free.app")
        input("Press Enter to exit...")
        return

    ngrok_path = os.path.join(os.path.dirname(__file__), "ngrok.exe")

    print(f"🚇 Starting ngrok with static domain: {ngrok_domain}")

    try:
        process = subprocess.Popen(
            [ngrok_path, "http", "5000", f"--domain={ngrok_domain}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        webhook_url = f"https://{ngrok_domain}/webhook"

        print("=" * 70)
        print("✅ ngrok started with STATIC domain!")
        print("=" * 70)
        print(f"🪝 Webhook URL: {webhook_url}")
        print("🔗 Dashboard: http://127.0.0.1:4040")
        print("=" * 70)

        # Keep running
        try:
            for line in process.stdout:
                print(line, end='')
        except KeyboardInterrupt:
            print("\n🛑 Stopping ngrok...")
            process.terminate()

    except Exception as e:
        print(f"❌ Error: {e}")
        input("Press Enter to exit...")


if __name__ == "__main__":
    main()