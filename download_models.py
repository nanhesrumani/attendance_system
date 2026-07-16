import requests
import os

os.makedirs("anti_spoof_models", exist_ok=True)

files = {
    "2.7_80x80_MiniFASNetV2.pth":
    "https://github.com/minivision-ai/Silent-Face-Anti-Spoofing/raw/master/resources/anti_spoof_models/2.7_80x80_MiniFASNetV2.pth",

    "4_0_0_80x80_MiniFASNetV1SE.pth":
    "https://github.com/minivision-ai/Silent-Face-Anti-Spoofing/raw/master/resources/anti_spoof_models/4_0_0_80x80_MiniFASNetV1SE.pth"
}

for filename, url in files.items():
    print(f"Downloading {filename}...")

    response = requests.get(url)

    if response.status_code == 200:
        with open(os.path.join("anti_spoof_models", filename), "wb") as f:
            f.write(response.content)

        print(f"Saved: {filename}")

    else:
        print(f"Failed to download {filename}")
        print("Status:", response.status_code)

print("Done")