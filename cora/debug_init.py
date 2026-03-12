
import sys
import time
import datetime

def log(msg):
    ts = datetime.datetime.now().strftime("%H:%M:%S.%f")
    print(f"[{ts}] [DEBUG] {msg}", flush=True)

try:
    log("Importing sys, time, threading...")
    import sys
    import time
    import threading
    
    log("Importing mss...")
    import mss
    log("mss imported.")
    
    log("Importing ollama...")
    import ollama
    log("ollama imported.")
    
    log("Importing PIL.Image...")
    from PIL import Image
    log("PIL.Image imported.")
    
    log("Importing config...")
    import config
    log("config imported.")
    
    log("Importing context_engine...")
    import context_engine
    log("context_engine imported.")
    
    log("Importing ocr_engine...")
    import ocr_engine
    log("ocr_engine imported.")
    
    log("Importing PyQt6.QtCore...")
    from PyQt6.QtCore import QObject, pyqtSignal
    log("PyQt6.QtCore imported.")
    
    log("Importing PyQt6.QtWidgets...")
    from PyQt6.QtWidgets import QApplication
    log("PyQt6.QtWidgets imported.")
    
    log("Importing docx...")
    import docx
    log("docx imported.")
    
    log("Importing pptx...")
    from pptx import Presentation
    log("pptx imported.")

    log("Importing openai...")
    try:
        from openai import OpenAI
        log("openai imported.")
    except Exception as e:
        log(f"openai import failed: {e}")

    log("Directly importing keyboard...")
    import keyboard
    log("keyboard imported.")
    
    log("All imports for observer successful!")
    
    log("Initializing QApplication...")
    app = QApplication(sys.argv)
    log("QApplication initialized.")
    
    log("Done.")
    
except Exception as e:
    log(f"CRASH: {e}")
    import traceback
    traceback.print_exc()
