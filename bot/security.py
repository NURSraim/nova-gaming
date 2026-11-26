"""
Nova Gaming — Модуль криптографической безопасности Telegram (HMAC-SHA256)
Отвечает за проверку подписи initData и распределение прав (RBAC).
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl, urlencode

# ==========================================
# 🛡️ СПИСКИ ДОСТУПА (ROLE-BASED ACCESS CONTROL)
# В продакшене они хранятся в Firestore, здесь базовый реестр
# ==========================================
SUPER_ADMIN_IDS: set[int] = {
    5619771997,  # Нурик (Root / Владелец платформы Nova Gaming)
    9999,        # Тестовый суперадмин для /dev полигона
}

CLUB_ADMIN_REGISTRY: dict[int, int] = {
    2001: 1,     # Telegram ID 2001 -> Админ Nova Cyber Arena (Клуб #1)
    2002: 2,     # Telegram ID 2002 -> Админ Next Level (Клуб #2)
}

MAX_FUTURE_SKEW_SECONDS = 60  # Допустимый рассинхрон часов сервера/клиента (60 сек)


def validate_init_data(
    init_data: str,
    bot_token: str,
    max_age_seconds: int = 86400,
) -> dict | None:
    """
    Проверяет криптографическую подпись строки Telegram.WebApp.initData.

    Алгоритм Telegram Core:
    1. Защита от Parameter Pollution: запрет дубликатов ключей.
    2. Ключ = HMAC-SHA256(key=b"WebAppData", data=BOT_TOKEN).
    3. Строка проверки = все пары key=value (кроме hash), отсортированные по алфавиту через \\n.
    4. Проверка HMAC-SHA256 через hmac.compare_digest ДО парсинга небезопасных полей.
    5. Проверка auth_date (не устарела и не из будущего).
    6. Строгая проверка структуры объекта user (обязателен целочисленный id).
    """
    if not isinstance(init_data, str) or not isinstance(bot_token, str):
        return None
    if not init_data.strip() or not bot_token.strip():
        return None

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None

    # Защита от HTTP Parameter Pollution (например: user=real&user=fake)
    parsed: dict[str, str] = {}
    for k, v in pairs:
        if k in parsed:
            return None
        parsed[k] = v

    received_hash = parsed.pop("hash", None)
    if not received_hash or len(received_hash) != 64:
        return None

    # 1. Сначала проверяем криптографическую подпись HMAC-SHA256
    sorted_items = sorted(parsed.items(), key=lambda x: x[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_items)

    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash.lower()):
        return None

    # 2. После подтверждения подписи проверяем свежесть auth_date (защита от Replay-атак)
    auth_date_raw = parsed.get("auth_date")
    if not auth_date_raw:
        return None
    try:
        auth_date = int(auth_date_raw)
    except ValueError:
        return None

    current_time = int(time.time())
    if auth_date <= 0:
        return None
    if current_time - auth_date > max_age_seconds:
        return None
    if auth_date - current_time > MAX_FUTURE_SKEW_SECONDS:
        return None

    # 3. Распаковываем и валидируем JSON пользователя Telegram
    user_raw = parsed.get("user")
    if not user_raw:
        return None
    try:
        user_data = json.loads(user_raw)
    except (ValueError, TypeError):
        return None

    if not isinstance(user_data, dict):
        return None
    user_id = user_data.get("id")
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return None

    return {
        "valid": True,
        "auth_date": auth_date,
        "query_id": parsed.get("query_id", ""),
        "user": user_data,
    }


def get_user_role(telegram_id: int) -> dict:
    """
    Определяет роль пользователя на основе проверенного целочисленного Telegram ID.
    Возвращает роль: 'sys' (суперадмин), 'admin' (админ клуба) или 'user' (игрок).
    """
    if not isinstance(telegram_id, int) or isinstance(telegram_id, bool) or telegram_id <= 0:
        return {"role": "user", "club_id": None, "title": "Игрок"}

    if telegram_id in SUPER_ADMIN_IDS:
        return {"role": "sys", "club_id": None, "title": "Главный Администратор Nova"}

    if telegram_id in CLUB_ADMIN_REGISTRY:
        club_id = CLUB_ADMIN_REGISTRY[telegram_id]
        return {"role": "admin", "club_id": club_id, "title": f"Администратор Клуба #{club_id}"}

    return {"role": "user", "club_id": None, "title": "Игрок"}


def create_mock_init_data(
    user_id: int,
    username: str,
    first_name: str,
    bot_token: str,
    age_offset: int = 0,
) -> str:
    """
    Генерирует криптографически валидную строку initData для локальных тестов.
    """
    auth_date = int(time.time()) - age_offset
    user_json = json.dumps(
        {
            "id": user_id,
            "first_name": first_name,
            "username": username,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )

    params = {
        "auth_date": str(auth_date),
        "query_id": "AAHd1234MockQuery",
        "user": user_json,
    }

    sorted_items = sorted(params.items(), key=lambda x: x[0])
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted_items)

    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    calc_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    params["hash"] = calc_hash
    return urlencode(params)


if __name__ == "__main__":
    print("=" * 60)
    print("🧪 РАСШИРЕННЫЙ АУДИТ БЕЗОПАСНОСТИ NOVA GAMING (8 СЦЕНАРИЕВ)")
    print("=" * 60)

    # ВАЖНО: В тестах используется ТОЛЬКО фейковый токен, никогда не реальный!
    FAKE_TEST_TOKEN = "123456789:FAKE_UNIT_TEST_TOKEN_DO_NOT_USE_IN_PROD"

    # 1. Честный админ клуба
    valid_str = create_mock_init_data(2001, "nova_admin", "Admin Timur", FAKE_TEST_TOKEN)
    r1 = validate_init_data(valid_str, FAKE_TEST_TOKEN)
    assert r1 is not None and get_user_role(r1["user"]["id"])["role"] == "admin"
    print("1. Честный админ клуба:             ✅ OK")

    # 2. Подмена ID хакером
    hacked_str = valid_str.replace("2001", "9999")
    assert validate_init_data(hacked_str, FAKE_TEST_TOKEN) is None
    print("2. Подмена ID в initData:           ✅ БЛОКИРОВАН")

    # 3. Протухший auth_date (Replay Attack)
    old_str = create_mock_init_data(1001, "player", "Nurik", FAKE_TEST_TOKEN, age_offset=90000)
    assert validate_init_data(old_str, FAKE_TEST_TOKEN, max_age_seconds=86400) is None
    print("3. Протухшая подпись (Replay):      ✅ БЛОКИРОВАН")

    # 4. auth_date из будущего (Clock tampering)
    future_str = create_mock_init_data(1001, "player", "Nurik", FAKE_TEST_TOKEN, age_offset=-3600)
    assert validate_init_data(future_str, FAKE_TEST_TOKEN) is None
    print("4. Дата из будущего (+1 час):       ✅ БЛОКИРОВАН")

    # 5. Битый auth_date (Попытка уронить сервер ValueError'ом)
    assert validate_init_data("auth_date=not_a_number&hash=" + "a" * 64, FAKE_TEST_TOKEN) is None
    print("5. Нечисловой auth_date (DoS):      ✅ БЛОКИРОВАН БЕЗ КРАША")

    # 6. HTTP Parameter Pollution (дубликат ключа user)
    polluted = valid_str + "&user=%7B%22id%22%3A9999%7D"
    assert validate_init_data(polluted, FAKE_TEST_TOKEN) is None
    print("6. Дубликат ключа (Pollution):      ✅ БЛОКИРОВАН")

    # 7. Подписание чужим токеном
    wrong_token_str = create_mock_init_data(9999, "hacker", "Evil", "000000:WRONG_TOKEN")
    assert validate_init_data(wrong_token_str, FAKE_TEST_TOKEN) is None
    print("7. Чужой BOT_TOKEN:                 ✅ БЛОКИРОВАН")

    # 8. Проверка get_user_role на невалидные типы
    assert get_user_role(True)["role"] == "user"  # bool является подклассом int в Python!
    assert get_user_role("9999")["role"] == "user"  # type: ignore
    print("8. Типовая защита get_user_role:    ✅ OK")

    print("=" * 60)
    print("🚀 ВСЕ 8 ПРОВЕРОК ПРОЙДЕНЫ БЕЗ ЕДИНОЙ ОШИБКИ!")
