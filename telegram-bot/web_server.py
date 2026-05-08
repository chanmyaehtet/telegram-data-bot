from flask import Flask
from threading import Thread
import os

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!", 200

@app.route('/health')
def health():
    return {"status": "ok"}, 200

def run():
    port = int(os.environ.get('BOT_KEEPALIVE_PORT', 6000))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

def keep_alive():
    t = Thread(target=run, daemon=True)
    t.start()
