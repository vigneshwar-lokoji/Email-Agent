import os
import requests
from dotenv import load_dotenv

# Load the keys from your hidden .env file
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def test_telegram():
    print("Attempting to contact the BotFather...")
    
    # The official Telegram API URL for sending messages
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    
    # The payload containing your specific ID and the message
    payload = {
        "chat_id": CHAT_ID,
        "text": "🤖 *System Online:* Bot is connected and ready.",
        "parse_mode": "Markdown"
    }
    
    # Fire the request
    response = requests.post(url, json=payload)
    
    if response.status_code == 200:
        print("Success! Check your phone.")
    else:
        print(f"Error: {response.text}")

if __name__ == '__main__':
    test_telegram()
