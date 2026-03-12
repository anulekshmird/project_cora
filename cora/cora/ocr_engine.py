import cv2
import numpy as np
import pytesseract
from PIL import Image
import os

# Default Tesseract Path (Windows)
# Users can override this if installed elsewhere
DEFAULT_TESSERACT_PATH = r"C:\Users\ADITHYA\AppData\Local\Programs\Tesseract-OCR\tesseract.exe"

def get_tesseract_path():
    # 1. Check default location
    if os.path.exists(DEFAULT_TESSERACT_PATH):
        return DEFAULT_TESSERACT_PATH
    
    # 2. Check PATH
    import shutil
    path = shutil.which("tesseract")
    if path:
        return path
        
    return None

# Configure Pytesseract
tess_path = get_tesseract_path()
if tess_path:
    pytesseract.pytesseract.tesseract_cmd = tess_path
else:
    print("Warning: Tesseract OCR not found. Please install via: https://github.com/UB-Mannheim/tesseract/wiki")

def extract_text(image_input):
    """
    Extracts text from an image (PIL Image or numpy array) using Tesseract OCR.
    Includes preprocessing for better accuracy.
    """
    if not tess_path:
        return ""

    try:
        # Convert PIL to OpenCV format
        if isinstance(image_input, Image.Image):
            point_img = image_input.convert('RGB')
            img_np = np.array(point_img)
            # Convert RGB to BGR for OpenCV
            img = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        elif isinstance(image_input, np.ndarray):
            img = image_input
        else:
            return ""

        # PREPROCESSING
        # 1. Grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # 2. Thresholding (Binary) - helps with sharp text
        # Using Otsu's thresholding for adaptive binarization
        _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # 3. Denoising (Optional, can hurt small text)
        # denoised = cv2.fastNlMeansDenoising(thresh, None, 10, 7, 21)

        # Run Tesseract
        # psm 3: Fully automatic page segmentation, but no OSD. (Default)
        # psm 6: Assume a single uniform block of text.
        custom_config = r'--oem 3 --psm 3' 
        text = pytesseract.image_to_string(thresh, config=custom_config)
        
        return text.strip()

    except Exception as e:
        print(f"OCR Error: {e}")
        return ""
