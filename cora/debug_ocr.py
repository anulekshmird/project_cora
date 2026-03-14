import os
import mss
from PIL import Image
from ocr_engine import extract_text, OCRMode

def test_ocr():
    print("--- Starting OCR Debug Test ---")
    
    with mss.mss() as sct:
        # Capture monitor 1
        monitor = sct.monitors[1]
        print(f"Capturing screen: {monitor['width']}x{monitor['height']}")
        sct_img = sct.grab(monitor)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        
        # Save for manual inspection if needed
        img.save("debug_screenshot.png")
        print("Screenshot saved to debug_screenshot.png")
        
        print("Running OCR (GENERAL mode)...")
        text = extract_text(img, mode=OCRMode.GENERAL)
        
        print(f"\nEXTRACTED TEXT ({len(text)} chars):")
        print("-" * 40)
        print(text[:1000]) # First 1000 chars
        print("-" * 40)
        
        if len(text.strip()) > 10:
            print("\nSUCCESS: OCR extracted meaningful text!")
        else:
            print("\nFAILURE: OCR returned very little or no text.")

if __name__ == "__main__":
    test_ocr()
