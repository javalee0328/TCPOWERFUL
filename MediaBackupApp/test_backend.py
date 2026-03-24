import requests
import os

os.makedirs("test_files", exist_ok=True)
test_file = "test_files/test_img.jpg"
with open(test_file, "wb") as f:
    f.write(b"fake image content 12345")

# Test 1: Upload endpoint
print("Testing /api/v1/upload...")
files = {'file': ('test_img.jpg', open(test_file, 'rb'), 'image/jpeg')}
resp = requests.post("http://127.0.0.1:8000/api/v1/upload?timestamp=2026-03-24T15:00:00Z&filename=test_img.jpg", files=files)
print(f"Upload response: {resp.status_code} {resp.text}")

# Test 2: Upload without filename in formData, only in URL (to simulate Expo)
print("\nTesting Expo simulation (no filename in multipart formData, only query param)...")
files2 = {'file': open(test_file, 'rb')}
resp2 = requests.post("http://127.0.0.1:8000/api/v1/upload?timestamp=2026-03-24T15:00:00Z&filename=expo_test_img.jpg", files=files2)
print(f"Expo Upload response: {resp2.status_code} {resp2.text}")

# Test 3: Gallery HTML (check if it renders properly)
print("\nTesting /gallery HTML...")
resp3 = requests.get("http://127.0.0.1:8000/gallery")
print(f"Gallery status: {resp3.status_code}")
if '<div class="thumb"' in resp3.text:
    print("Gallery rendered thumbs successfully!")

# Test 4: Dynamic file serving /view/{path}
print("\nTesting /view/{path} dynamic serving...")
resp4 = requests.get("http://127.0.0.1:8000/view/2026/03/expo_test_img.jpg")
print(f"View status: {resp4.status_code}")
if resp4.status_code == 200:
    print("Dynamic serving successfully read the file!")

# Test 5: Check empty file deletion logic
print("\nTesting duplicate check with size=0 bug...")
open("storage/2026/03/empty.jpg", "wb").close()
resp5 = requests.get("http://127.0.0.1:8000/api/v1/check?filename=empty.jpg&size=100")
print(f"Check empty file response (should delete it and say it doesn't exist): {resp5.text}")

