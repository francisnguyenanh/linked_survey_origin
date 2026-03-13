"""Entry point for the DSAF (Dynamic Survey Automation Framework) application."""

import eventlet
eventlet.monkey_patch()

import eventlet
eventlet.monkey_patch()

from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.extensions import socketio

app = create_app()

if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5010, debug=True, use_reloader=False)
