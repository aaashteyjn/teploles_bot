import os
import json
import re
from dotenv import load_dotenv

import telebot
from openai import OpenAI

import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime

# Загружаем .env
load_dotenv()


def clean_env(name: str, default: str | None = None) -> str | None:
    """
    Берём переменную окружения и убираем лишние пробелы/кавычки.
    Чтобы PROXY_API_KEY='sk-xxx' не превращался в "'sk-xxx'".
    """
    val = os.getenv(name, default)
    if val is None:
        return None
    return val.strip().strip('"').strip("'")

# --------- ЛОГИРОВАНИЕ ---------

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# формат логов
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# технические логи бота
bot_log_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "bot.log"),
    maxBytes=1_000_000,     # ~1 МБ
    backupCount=5,
    encoding="utf-8",
)
bot_log_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

logging.basicConfig(
    level=logging.INFO,
    handlers=[bot_log_handler],
)

logger = logging.getLogger("teploles.bot")

# отдельный лог диалогов (для админа)
dialogs_handler = RotatingFileHandler(
    os.path.join(LOG_DIR, "dialogs.log"),
    maxBytes=2_000_000,
    backupCount=3,
    encoding="utf-8",
)
dialogs_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

dialogs_logger = logging.getLogger("teploles.dialogs")
dialogs_logger.setLevel(logging.INFO)
dialogs_logger.addHandler(dialogs_handler)


def log_dialog(chat_id: int, user_text: str, answer: str) -> None:
    """
    Пишем в отдельный файл простую историю диалогов:
    кто, что спросил и что ответил бот.
    """
    dialogs_logger.info(
        "chat=%s | USER: %s | BOT: %s",
        chat_id,
        user_text.replace("\n", " "),
        answer.replace("\n", " "),
    )

# --------- ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ---------

TELEGRAM_BOT_TOKEN = clean_env("TELEGRAM_BOT_TOKEN")
PROXY_API_KEY = clean_env("PROXY_API_KEY")
MODEL_NAME = clean_env("MODEL_NAME") or "gpt-4o-mini"
OPENAI_BASE_URL = clean_env("OPENAI_BASE_URL") 

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не найден или пустой в .env")

if not PROXY_API_KEY:
    raise RuntimeError("PROXY_API_KEY не найден или пустой в .env")

# --------- КЛИЕНТ OpenAI ЧЕРЕЗ ПРОКСИ ---------

openai_kwargs = {"api_key": PROXY_API_KEY}
if OPENAI_BASE_URL:
    openai_kwargs["base_url"] = OPENAI_BASE_URL

print("OpenAI client config:", openai_kwargs)  # можно отключить после отладки

client = OpenAI(**openai_kwargs)

# --- загружаем БД товаров из products.json ---
with open("products.json", "r", encoding="utf-8") as f:
    PRODUCT_DB = json.load(f)["products"]

# --- Telegram bot ---
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# память диалогов
CONVERSATIONS: dict[int, list[dict]] = {}
MAX_HISTORY = 50

# корзина: chat_id -> список позиций
# позиция:
# {
#   "product_id", "name", "packaging", "quantity",
#   "unit", "unit_price", "unit_price_desc", "subtotal"
# }
CARTS: dict[int, list[dict]] = {}

SYSTEM_PROMPT = """
Ты — вежливый и полезный ИИ-ассистент компании «Теплолес».

Мы продаём:
- древесные пеллеты (белые «Премиум», серые «Капучино»),
- торфяные брикеты «Люкс»,
- древесные брикеты (Pini Kay, RUF),
- уголь и древесный уголь,
- кошачий наполнитель,
- теплоносители МЭГ и ПГ.

Тебе ВСЕГДА передают выдержку из каталога товаров и (иногда) рассчитанные ботом суммы в сообщении ассистента перед вопросом пользователя.
Эти данные важнее твоих догадок — ИХ НЕЛЬЗЯ менять.

Ключевые правила:

1. ЦЕНЫ
   - Если в каталоге или в расчёте прямо для товара указана цена или текст price_note/Стоимость или сумма, ты ОБЯЗАН использовать именно эти числа.
   - Нельзя придумывать свои цены или тарифы, менять цифры или сильно округлять их.
   - Если цена не указана, честно скажи, что точной цены нет и менеджер её уточнит.

2. ФАСОВКА
   - Фасовка товара всегда перечислена в каталоге (мешки, биг-бэги, поддоны и т.п.).
   - Разрешено упоминать ТОЛЬКО те варианты фасовки, которые прямо перечислены в каталоге.
   - Нельзя придумывать дополнительные фасовки, которых нет в данных.
   - Если у товара только ОДНА фасовка — используй её по умолчанию и не спрашивай «в какой фасовке». Можно уточнить только количество.

3. ДОСТАВКА:
    - Базовый адрес: Воскресенск, ул. Гагарина, д. 38 (самовывоз).
    - Перед тем как просить расстояние или считать доставку, СНАЧАЛА уточни у клиента:
    «Вам нужна доставка или планируете самовывоз из г. Воскресенск, ул. Гагарина, 38?».
    - Если клиент выбирает самовывоз — НЕ проси километраж и НЕ считай доставку, просто уточни удобное время.
    - Если клиент выбирает доставку, попроси назвать примерный километраж от его адреса до нашей базы в Воскресенске (например, 50 км).
    - Доставка считается по формуле: 
    • минимальная стоимость доставки 1000 руб (до 25 км включительно),
    • далее 40 руб за каждый километр от базы.
    - Если ассистент даёт тебе расстояние и рассчитанную стоимость доставки — пользуйся именно этими данными и не меняй их.
    - Не придумывай другие тарифы и не изменяй логику расчёта.

4. ВОПРОСЫ КЛИЕНТУ
   - При вопросах о цене товара уточняй объём и фасовку (если фасовки несколько) и город/расстояние до точки доставки.
   - Если клиент уже указал фасовку, не переспроси про неё ещё раз, а уточни только количество и город/расстояние.

5. ОБРАЩЕНИЕ
   - Отвечай по-русски, коротко и по делу, дружелюбным тоном.
   - В конце ответа старайся задать один уточняющий вопрос, который двигает к заказу (объём, доставка/самовывоз, расстояние, удобный способ связи и т.п.).

Если каких-то данных (цена, характеристика, фасовка, стоимость доставки) нет в каталоге, прямо скажи, что точной информации нет, и предложи связаться с менеджером.
""".strip()

# --- ДОПОЛНИТЕЛЬНЫЕ ПРАВИЛА ДЛЯ КОРЗИНЫ И ДОСТАВКИ ---
SYSTEM_PROMPT = SYSTEM_PROMPT + """
Дополнительные правила по корзине и доставке:

- Перед ответом ты можешь получить сводку корзины (строки вида «Текущая корзина заказа...»).
  В ней перечислены товары, их количество и ориентировочная сумма. Эти числа НЕЛЬЗЯ менять.
- В итоговой сумме учитывай все позиции из корзины, а не только последний товар.
- Если к корзине добавлена стоимость доставки, учитывай её в общем итоговом подсчёте.
- Никогда не выкидывай уже добавленные товары из корзины, можешь только переобсудить заказ с клиентом.

- Для расчёта доставки НЕ достаточно знать только город.
  Объясни, что наша база находится в г. Воскресенск, ул. Гагарина, д. 38,
  и попроси клиента указать примерный километраж от его адреса до нашей базы (например, «50 км»).
"""


# ---------- Поиск товаров в БД ----------

def find_relevant_products(text: str):
    """Ищем товары по категориям и ключевым словам."""
    text = text.lower()
    results = []

    # 1) По категориям
    if "пеллет" in text:
        results.extend([p for p in PRODUCT_DB if p.get("category") == "pellets"])

    if "торф" in text or "торфя" in text:
        results.extend([p for p in PRODUCT_DB if p.get("category") == "peat_briquette"])

    if "брикет" in text:
        results.extend([p for p in PRODUCT_DB if p.get("category") in ("briquette", "peat_briquette")])

    if "уголь" in text:
        results.extend([p for p in PRODUCT_DB if p.get("category") in ("coal", "charcoal")])

    if "наполнител" in text or "туалет" in text:
        results.extend([p for p in PRODUCT_DB if p.get("category") == "cats_litter"])

    if "мэг" in text or "моноэтилен" in text:
        results.extend([p for p in PRODUCT_DB if p.get("category") == "heat_carrier" and "МЭГ" in p.get("name", "")])

    if "пг" in text or "пропиленгликоль" in text:
        results.extend([p for p in PRODUCT_DB if p.get("category") == "heat_carrier" and "ПГ" in p.get("name", "")])

    # 2) По ключевым словам 
    for p in PRODUCT_DB:
        name_l = p["name"].lower()
        keywords = [name_l] + p.get("keywords", [])
        if any(kw and kw.lower() in text for kw in keywords):
            if p not in results:
                results.append(p)

    return results

# ---------- Парсинг количества ----------

def parse_quantity(text: str):
    """
    Очень простой парсер количества.
    Возвращает (qty: int | None, unit: str | None), где unit: bag/pallet/kg/ton/None.
    """
    t = text.lower()

    m = re.search(r'(\d+)\s*(мешк|мешка|мешков|упаковк|упак|пакет|пачк)', t)
    if m:
        return int(m.group(1)), "bag"

    m = re.search(r'(\d+)\s*(поддон|поддона|поддонов|паллет)', t)
    if m:
        return int(m.group(1)), "pallet"

    m = re.search(r'(\d+)\s*(кг|килограмм)', t)
    if m:
        return int(m.group(1)), "kg"

    m = re.search(r'(\d+)\s*(т|тонн|тонна|тонны)', t)
    if m:
        return int(m.group(1)), "ton"

    # просто число — будем считать количеством без явной единицы
    m = re.fullmatch(r'\s*(\d+)\s*', t)
    if m:
        return int(m.group(1)), None

    return None, None

# ---------- Парсинг расстояния ----------

def parse_distance_km(text: str):
    """
    Ищем в тексте расстояние в км.
    Возвращаем float км или None.
    """
    t = text.lower().replace(',', '.')
    m = re.search(r'(\d+(?:\.\d+)?)\s*(км|kilometer|kilometre|km)', t)
    if m:
        return float(m.group(1))

    # если сообщение — просто число, считаем что это км
    if re.fullmatch(r'\s*\d+(?:\.\d+)?\s*', t):
        try:
            return float(t.strip())
        except Exception:
            pass

    return None

def is_self_pickup(text: str) -> bool:
    t = text.lower()
    return any(word in t for word in [
        "самовывоз",
        "заберу сам",
        "заберем сами",
        "приеду сам",
        "приедем сами",
        "сам",
        "сами"
    ])

def calc_delivery_cost(km: float) -> int:
    """
    Формула: min 1000 руб, дальше 40 руб/км.
    """
    if km <= 25:
        return 1000
    return int(round(km * 40))

# ---------- Вспомогательные функции для корзины ----------

def _pick_variant_for_unit(product: dict, unit: str | None):
    variants = product.get("variants") or product.get("варианты фасовки") or []
    if not variants:
        return None

    unit = unit or ""

    if unit == "bag":
        for v in variants:
            pack = (v.get("packaging") or v.get("Фасовка") or "").lower()
            if "меш" in pack or "упаков" in pack or "упак" in pack or "пакет" in pack or "пачк" in pack:
                return v

    if unit == "pallet":
        for v in variants:
            pack = (v.get("packaging") or v.get("Фасовка") or "").lower()
            if "поддон" in pack or "паллет" in pack:
                return v

    # если ничего не подошло — берём первый
    return variants[0] if variants else None

def _extract_price_from_variant(variant: dict):
    """
    Пробуем вытащить цену из текстового поля price_note/Стоимость/цена.
    Берём первое число перед 'руб'.
    """
    note = (
        variant.get("price_note")
        or variant.get("Стоимость")
        or variant.get("цена")
        or ""
    )
    m = re.search(r'(\d+)\s*руб', note)
    if not m:
        return None, None
    price = int(m.group(1))
    return price, f"{price} руб/шт. (по каталогу)"

def add_to_cart_if_applicable(chat_id: int, user_text: str):
    """
    Пытаемся понять, что пользователь оформляет заказ на конкретный товар,
    и, если да, добавляем позицию в корзину.
    """
    # если сообщение про километраж, не трактуем его как заказ
    if parse_distance_km(user_text) is not None and "км" in user_text.lower():
        return

    qty, unit = parse_quantity(user_text)
    if qty is None:
        return

    # смотрим контекст последних сообщений пользователя (до 5 реплик + текущее),
    # чтобы понять, о каком товаре речь
    history = CONVERSATIONS.get(chat_id, [])
    user_texts = [m["text"] for m in history if m["speaker"] == "user"]
    user_texts.append(user_text)
    combined_text = " ".join(user_texts[-5:])

    products = find_relevant_products(combined_text)
    if not products:
        return

    product = products[0]
    variant = _pick_variant_for_unit(product, unit)
    if not variant:
        return
    
    price, unit_price_desc = _extract_price_from_variant(variant)

    packaging = (variant.get("packaging") or variant.get("Фасовка") or "").lower()

    subtotal = price * qty if price is not None else None

    CARTS.setdefault(chat_id, []).append(
        {
            "product_id": product.get("id"),
            "name": product.get("name"),
            "packaging": packaging,
            "quantity": qty,
            "unit": unit,
            "unit_price": price,
            "unit_price_desc": unit_price_desc,
            "subtotal": subtotal,
        }
    )

def build_cart_summary_lines(chat_id: int):
    """
    Формируем текстовую сводку корзины и возвращаем (lines, total_known).
    """
    cart = CARTS.get(chat_id, [])
    lines: list[str] = []
    if not cart:
        return lines, None

    lines.append("Текущая корзина заказа (сформирована ботом на основе реплик клиента):")
    total_known = 0
    have_unknown = False

    for i, item in enumerate(cart, start=1):
        name = item["name"] or "товар"
        qty = item["quantity"]
        unit = item["unit"] or ""
        pkg = item["packaging"] or ""
        unit_price_desc = item["unit_price_desc"]
        subtotal = item["subtotal"]

        if unit == "bag":
            unit_str = "меш."
        elif unit == "pallet":
            unit_str = "подд."
        elif unit == "kg":
            unit_str = "кг"
        elif unit == "ton":
            unit_str = "т"
        else:
            unit_str = ""

        base = f"{i}) {name}"
        if pkg:
            base += f", фасовка: {pkg}"
        base += f", количество: {qty} {unit_str}".strip()

        if unit_price_desc:
            base += f", цена: {unit_price_desc}"
        if subtotal is not None:
            base += f", сумма: ≈ {subtotal} руб."
            total_known += subtotal
        else:
            base += ", сумма по этому товару уточняется по прайсу."
            have_unknown = True

        lines.append(base)

    if total_known > 0:
        lines.append(f"Ориентировочная сумма по товарам с известной ценой: ≈ {total_known} руб.")
    if have_unknown:
        lines.append("Часть товаров без точной цены — их стоимость уточнит менеджер по актуальному прайсу.")

    return lines, total_known

def build_order_calculation_lines(chat_id: int, products: list, combined_text: str):
    """
    Добавляем в контекст информацию по корзине и доставке.
    """
    lines: list[str] = []

    # 1) корзина
    cart_lines, total_known = build_cart_summary_lines(chat_id)
    if cart_lines:
        lines.extend(cart_lines)
        lines.append("")

    # 2) доставка
    km = parse_distance_km(combined_text)
    delivery = None
    if km is not None:
        delivery = calc_delivery_cost(km)
        lines.append(
            f"Расчёт доставки: примерное расстояние {km:.0f} км от базы "
            f"(г. Воскресенск, ул. Гагарина, 38). "
            f"По формуле: минимум 1000 руб до 25 км, далее 40 руб/км. "
            f"Стоимость доставки ≈ {delivery} руб."
        )

    # 3) итог
    if total_known is not None and total_known > 0:
        if delivery is not None:
            total = total_known + delivery
            lines.append(
                f"Итого ориентировочная сумма по товарам с известной ценой и доставкой: ≈ {total} руб."
            )
        else:
            lines.append(
                f"Ориентировочная сумма по товарам с известной ценой (без учёта доставки): ≈ {total_known} руб."
            )

    if not cart_lines and km is not None and not products:
        lines.append(
            "По товарам в корзине сейчас ничего нет, поэтому считается только доставка."
        )

    return lines

# ---------- Контекст каталога ----------

def build_catalog_context(chat_id: int, user_text: str) -> str:
    """
    Смотрим на последние несколько сообщений пользователя + текущее,
    находим подходящие товары и формируем описание каталога.
    """
    history = CONVERSATIONS.get(chat_id, [])
    user_texts = [m["text"] for m in history if m["speaker"] == "user"]
    user_texts.append(user_text)
    combined_text = " ".join(user_texts[-5:])  # последние 5 реплик пользователя

    products = find_relevant_products(combined_text)

    if not products:
        return (
            "По последним сообщениям пользователя в каталоге не найдено точных совпадений. "
            "Если вопрос про конкретный товар — уточни его название. "
            "Не придумывай цены и фасовки, которых нет в каталоге."
        )

    lines = ["Вот данные каталога, на которые нужно опираться при ответе:"]

    for p in products:
        lines.append(f"\nТовар: {p['name']}")
        if p.get("category"):
            lines.append(f"Категория: {p['category']}")

        # Варианты фасовки и цен
        variants = p.get("variants") or p.get("варианты фасовки") or []
        if variants:
            lines.append("Фасовка и цены/комментарии:")
            for v in variants:
                packaging = (
                    v.get("packaging")
                    or v.get("Фасовка")
                    or v.get("фасовка")
                    or ""
                )
                note = (
                    v.get("price_note")
                    or v.get("Стоимость")
                    or v.get("цена")
                    or ""
                )
                lines.append(f"- {packaging}: {note}")

            if len(variants) == 1:
                lines.append(
                    "Важно: у этого товара только ОДНА фасовка (перечислена выше). "
                    "Других вариантов фасовки нет, не придумывай их."
                )
            else:
                lines.append(
                    "Важно: у этого товара есть только перечисленные выше варианты фасовки. "
                    "Никаких других фасовок у этого товара нет."
                )

        # Характеристики
        specs = p.get("specs", {})
        if specs:
            lines.append("Характеристики:")
            if specs.get("material"):
                lines.append(f"- материал: {specs['material']}")
            if specs.get("color"):
                lines.append(f"- цвет: {specs['color']}")
            if specs.get("diameter_mm"):
                lines.append(f"- диаметр: {specs['diameter_mm']} мм")
            if specs.get("quality"):
                lines.append(f"- качество: {specs['quality']}")
            if specs.get("ash_percent"):
                lines.append(f"- зольность: {specs['ash_percent']}%")
            if specs.get("moisture_percent"):
                lines.append(f"- влажность: {specs['moisture_percent']}%")
            if specs.get("calorific"):
                lines.append(f"- теплота сгорания: {specs['calorific']}")
            if specs.get("density"):
                lines.append(f"- плотность: {specs['density']}")
            if specs.get("description"):
                lines.append(f"- описание: {specs['description']}")
            if specs.get("size"):
                lines.append(f"- размер: {specs['size']}")

        lines.append(
            "При ответе по этому товару используй ИМЕННО эти характеристики и цены. "
            "Не придумывай новые числа и не меняй указанные значения."
        )
    
    # добавляем информацию по корзине и доставке
    extra_lines = build_order_calculation_lines(chat_id, products, combined_text)
    if extra_lines:
        lines.append("\nДополнительная информация по корзине и доставке:")
        lines.extend(extra_lines)

    return "\n".join(lines)


# ---------- Формирование messages для модели ----------

def build_messages(chat_id: int, user_text: str):
    history = CONVERSATIONS.get(chat_id, [])

    messages = []
    messages.append({"role": "system", "content": SYSTEM_PROMPT})

    catalog_context = build_catalog_context(chat_id, user_text)
    messages.append({"role": "assistant", "content": catalog_context})

    for item in history[-MAX_HISTORY:]:
        role = "user" if item["speaker"] == "user" else "assistant"
        messages.append({"role": role, "content": item["text"]})

    messages.append({"role": "user", "content": user_text})

    return messages


# ---------- Вызов модели ----------

def ask_llm(chat_id: int, user_text: str) -> str:
    # сначала пробуем добавить товар в корзину (если это заказ)
    add_to_cart_if_applicable(chat_id, user_text)

    CONVERSATIONS.setdefault(chat_id, []).append(
        {"speaker": "user", "text": user_text}
    )

    messages = build_messages(chat_id, user_text)

    try:
        completion = client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            temperature=0.1,  # меньше креатива
        )
        answer = completion.choices[0].message.content.strip()
    except Exception as e:
        logger.exception("OpenAI ERROR")  # пишем стектрейс в bot.log
        answer = (
            "Сейчас не получается получить ответ от ассистента. "
            "Попробуйте, пожалуйста, чуть позже."
        )

    CONVERSATIONS[chat_id].append(
        {"speaker": "assistant", "text": answer}
    )

    # 💾 логируем диалог для админа
    try:
        log_dialog(chat_id, user_text, answer)
    except Exception as e:
        # чтобы ошибка логирования не убила бота
        logger.exception("Ошибка при логировании диалога")

    return answer


# ---------- Telegram handlers ----------

@bot.message_handler(commands=["start"])
def handle_start(message: telebot.types.Message):
    logger.info("User %s started bot", message.chat.id)
    chat_id = message.chat.id

    # очищаем историю и корзину для нового диалога
    CONVERSATIONS[chat_id] = []
    CARTS[chat_id] = []

    bot.send_message(
        chat_id,
        "Здравствуйте! 👋\n"
        "Я ассистент Теплолес.\n"
        "Помогу с выбором топлива (пеллеты, брикеты, торф, уголь) и "
        "ориентировочным расчётом стоимости доставки.\n\n"
    )

@bot.message_handler(content_types=["text"])
def handle_text(message: telebot.types.Message):
    chat_id = message.chat.id
    user_text = (message.text or "").strip()
    if not user_text:
        return
    logger.info("New message from %s: %s", chat_id, user_text)

    answer = ask_llm(chat_id, user_text)
    try:
        bot.send_message(chat_id, answer)
    except Exception as e:
        print("Ошибка при отправке ответа пользователю:", e)


# ---------- запуск ----------

if __name__ == "__main__":
    print("Teploles bot with product DB & cart is running...")
    bot.infinity_polling(skip_pending=True)