import os
import sys
import time
from dotenv import load_dotenv
from PyQt6.QtCore import QCoreApplication

# Load from .env file
load_dotenv()

from ai_engine import AIEngine
from context_extractor import Context

app = QCoreApplication(sys.argv)
engine = AIEngine()

# Create a mock context
ctx = Context(app="editor", window_title="test.py", source="window")
ctx.visible_text = "def hello_world():\n    print('Hello World!')\n"

def on_suggestion(payload):
    print("\nSUCCESS! Received Suggestion from Gemini:")
    print(payload)
    app.quit()
    sys.exit(0)

def on_error(err):
    print("\nERROR! Gemini failed:")
    print(err)
    app.quit()
    sys.exit(1)

engine.suggestion_ready.connect(on_suggestion)
engine.error_occurred.connect(on_error)

print("Sending context to Gemini...")
engine.generate_suggestion_async(ctx)

# Start event loop to process signals
app.exec()
