import json, os, urllib.parse, urllib.request, sys
from pathlib import Path

# Впиши сюда свой токен для проверки прямо сейчас (будет удален после теста)
TOKEN = os.getenv("VK_TOKEN") or ""
API = "https://api.vk.com/method"
V = "5.131"

# Тот самый набор UA из коммита недельной давности
PROBE_UAS = [
    "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)",
    "KateMobileAndroid/56 lite-460 (Android 11; SDK 30; arm64-v8a; samsung SM-G998B; ru)",
    "VKAndroidApp/8.17-15822 (Android 14; SDK 34; arm64-v8a; Google Pixel 8; ru) VKAndroidApp/8.17-15822",
]

def vk_call(method, token, ua):
    params = {"access_token": token, "v": V, "q": "beatles", "count": 1}
    url = f"{API}/{method}?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": ua}
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))

if __name__ == "__main__":
    if not TOKEN:
        print("Ошибка: Токен не задан. Задай в переменную окружения VK_TOKEN.")
        sys.exit(1)
        
    for ua in PROBE_UAS:
        try:
            res = vk_call("audio.search", TOKEN, ua)
            if "error" in res:
                print(f"UA={ua[:30]}... -> ERR={res['error']['error_code']} {res['error']['error_msg']}")
            else:
                print(f"UA={ua[:30]}... -> SUCCESS!")
        except Exception as e:
            print(f"UA={ua[:30]}... -> EXC={e}")
