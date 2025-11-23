from flask import Flask, jsonify
import threading
import time
import os

app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "online", 
        "service": "HinariAdsBot",
        "timestamp": time.time(),
        "message": "Bot is running on Render!"
    })

@app.route('/health')
def health():
    return jsonify({"status": "healthy"})

def run_web():
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, debug=False)

def keep_alive():
    server_thread = threading.Thread(target=run_web)
    server_thread.daemon = True
    server_thread.start()
