"""
Запусти этот скрипт ОДИН РАЗ — он откроет сайт в браузере (Chromium),
перехватит все сетевые запросы и покажет реальные API endpoints с данными.

Запуск:
    python discover_api.py

После — скопируй нужные URL в monitor.py
"""

import asyncio
import json
from playwright.async_api import async_playwright


SITE_URL = "https://mostanet.ru"
IGNORE_DOMAINS = {"mc.yandex.ru", "fonts.googleapis.com", "fonts.gstatic.com",
                  "webim.ru", "vk.com", "connect.facebook.net"}


def is_interesting(url: str) -> bool:
    for d in IGNORE_DOMAINS:
        if d in url:
            return False
    # Ищем JSON-подобные ответы от самого сайта
    return "mostanet.ru" in url and url != SITE_URL + "/"


async def main():
    print("=== Запускаю браузер и перехватываю запросы ===")
    print(f"Открываю: {SITE_URL}\n")

    captured = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)  # headless=False — виден браузер
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        async def on_response(response):
            url = response.url
            if not is_interesting(url):
                return
            try:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type:
                    body = await response.json()
                    captured.append({"url": url, "status": response.status, "body": body})
                    print(f"[API] {response.status} {url}")
                    print(json.dumps(body, ensure_ascii=False, indent=2)[:800])
                    print("---")
                else:
                    captured.append({"url": url, "status": response.status, "body": None})
                    print(f"[REQ] {response.status} {url}")
            except Exception as e:
                print(f"[ERR] {url} — {e}")

        page.on("response", on_response)

        await page.goto(SITE_URL, wait_until="networkidle", timeout=30000)
        print("\n>>> Страница загружена. Взаимодействуй с формой поиска в браузере.")
        print(">>> Выбери направление, дату и нажми Поиск — увижу запросы.")
        print(">>> Нажми Enter здесь когда закончишь...\n")

        # Ждём пока пользователь взаимодействует с сайтом
        await asyncio.get_event_loop().run_in_executor(None, input)

        print("\n=== Итоговый список перехваченных API запросов ===")
        api_calls = [c for c in captured if c["body"] is not None]
        for item in api_calls:
            print(f"\nURL: {item['url']}")
            print(f"Status: {item['status']}")
            preview = json.dumps(item["body"], ensure_ascii=False, indent=2)
            print(preview[:1000])

        # Сохраняем результаты
        with open("captured_api.json", "w", encoding="utf-8") as f:
            json.dump(api_calls, f, ensure_ascii=False, indent=2)
        print("\nВсе запросы сохранены в captured_api.json")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
