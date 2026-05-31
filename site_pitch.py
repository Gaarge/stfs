import argparse
import os
import re
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

MODEL = "google/gemini-3-flash-preview"


def fetch_site_text(url: str, max_chars: int = 8000) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; SiteAnalyzer/1.0)"
    }

    response = requests.get(url, headers=headers, timeout=15)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    meta_description = ""
    meta = soup.find("meta", attrs={"name": "description"})
    if meta and meta.get("content"):
        meta_description = meta["content"].strip()

    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip()

    result = f"""
URL: {url}
Title: {title}
Meta description: {meta_description}
Page text: {text}
""".strip()

    return result[:max_chars]


def generate_pitch(url: str, site_text: str, found_page: int | None = None) -> str:
    if found_page:
        search_context = (
            f"Я нашёл этот сайт, когда копался в интернете, "
            f"и он попался мне только на {found_page}-й странице поисковика."
        )
    else:
        search_context = "Я нашёл этот сайт, когда копался в интернете."

    prompt = f"""
Ты пишешь короткое сообщение потенциальному клиенту от веб-разработчика.

Стиль:
- Пиши просто, по-человечески.
- Без пафоса.
- Без слов: "статусный", "экспертиза", "упаковать", "презентабельный", "визуальная форма", "достойный", "серьезные клиенты".
- Не используй канцелярит.
- Не пиши слишком вежливо и корпоративно.
- Тон: прямой, уверенный, но без оскорблений.
- Максимум 5-6 предложений.

Что нужно сказать:
1. Я нашёл сайт, когда копался в интернете.
2. Если указан номер страницы поиска, скажи, что сайт показывается только на этой странице.
3. Объясни простыми словами, что сайт выглядит плохо/устаревше/неудобно и из-за этого может отталкивать клиентов.
4. Скажи, что такая низкая позиция в поиске часто бывает из-за того, что сайт изначально плохо спроектирован: плохая структура, слабый текст, старый дизайн, мало понятных блоков.
5. Скажи, что я могу сделать новый сайт: красивый, понятный, современный и нормально подготовленный под поиск.
6. Ни слова не говори про цену.
7. Напиши, что меня зовут Владислав, я уже несколько лет разрабатываю сайты, занимаюсь SEO-анализом - тоесть слежу за тем, чтобы сайт увидели как можно больше целевых клиентов
8. Разрабатывал сайты для учебных заведений, адвокатских контор, интернет магазинов, если нужно, то могу прислать свои работы
9. Работаю по договору
Напиши уверенно, но честно: "сайт будет сделан так, чтобы он часто попадал на первые страницах поиска".
И объясни, что из за того, что он будет чаще попадать на первые страницы, то его сможет увидеть больше людей, поэтому у Вас будет больше клиентов
Еще можешь немного, пару слов совсем рассказать почему именно этот сайт плохой
НЕ ЗАБУДЬ ПОЗДОРОВАТЬСЯ! НО ПРОЩАТЬСЯ НЕ НАДО!
Контекст поиска:
{search_context}

Данные сайта:
{site_text}

Выдай только готовое сообщение клиенту, без пояснений.
""".strip()

    if not OPENROUTER_API_KEY:
        raise RuntimeError(
            "Не найден OPENROUTER_API_KEY. Добавь его в .env или экспортируй переменную окружения."
        )

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            # Необязательно, но OpenRouter рекомендует для идентификации приложения:
            "HTTP-Referer": "https://example.com",
            "X-Title": "Site Pitch Generator",
        },
        json={
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
            "temperature": 0.7,
            "max_tokens": 300,
        },
        timeout=60,
    )

    if response.status_code != 200:
        raise RuntimeError(f"OpenRouter error {response.status_code}: {response.text}")

    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="URL сайта для анализа")
    parser.add_argument(
        "--page",
        type=int,
        default=None,
        help="Страница поисковика, на которой сайт был реально найден"
    )

    args = parser.parse_args()

    try:
        site_text = fetch_site_text(args.url)
        pitch = generate_pitch(args.url, site_text, args.page)

        print("\n--- Сообщение клиенту ---\n")
        print(pitch)

    except requests.RequestException as e:
        print(f"Ошибка при загрузке сайта: {e}")
    except Exception as e:
        print(f"Ошибка: {e}")


if __name__ == "__main__":
    main()
