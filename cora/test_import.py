import google
print(f"Google path: {google.__path__}")
try:
    from google import genai
    print("Import success: from google import genai")
except ImportError as e:
    print(f"Import fail: {e}")

try:
    import google.genai
    print("Import success: import google.genai")
except ImportError as e:
    print(f"Import fail: {e}")
