import time
import requests

# === НАСТРОЙКИ АВТОРИЗАЦИИ SELECTEL ===
ACCOUNT_ID = "728238"  # Твой 6-значный номер аккаунт
PROJECT_ID = "d99378ada60c43d28971ee8386c6cce6"  # UUID проекта в Облачной платформе
USERNAME = "Brooklyn"  # Логин сервисного пользователя
PASSWORD = "aX7AZK]+0:HTQ, [M%$Fy"  # Пароль сервисного пользователя

# === НУЖНЫЕ ПРЕФИКСЫ IP ===
TARGET_PREFIXES = (
    "81.163.22.",
    "81.163.23.",
    "87.228.32.",
    "87.228.33.",
    "87.228.101.",
    "185.91.52.",
    "37.9.4.",
    "31.129.42.",
    "46.182.24.",
    "5.178.85.",
)

MAX_ATTEMPTS_PER_RUN = 500  # Общее число попыток за запуск
ATTEMPTS_PER_ZONE = (
    10  # Сколько попыток подряд делать в одной зоне до переключения
)
DELAY_BETWEEN_ATTEMPTS = 5  # Пауза между попытками (сек)

# Настройки Telegram (обязательно заполни для уведомлений)
TG_BOT_TOKEN = "8807138880:AAHVhJEo27eA9atiFBy4aFP987i7A73tOy0"
TG_CHAT_ID = "1655842429"

AUTH_URL = "https://cloud.api.selcloud.ru/identity/v3/auth/tokens"


def send_tg_message(text):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url, json={"chat_id": TG_CHAT_ID, "text": text}, timeout=10
        )
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")


def is_target_ip(ip_str):
    return ip_str.startswith(TARGET_PREFIXES)


def get_keystone_session():
    payload = {
        "auth": {
            "identity": {
                "methods": ["password"],
                "password": {
                    "user": {
                        "name": USERNAME,
                        "domain": {"name": str(ACCOUNT_ID)},
                        "password": PASSWORD,
                    }
                },
            },
            "scope": {"project": {"id": PROJECT_ID}},
        }
    }
    resp = requests.post(AUTH_URL, json=payload, timeout=20)
    resp.raise_for_status()

    token = resp.headers.get("X-Subject-Token")
    catalog = resp.json().get("token", {}).get("catalog", [])

    regions_map = {}
    for service in catalog:
        if service.get("type") == "network":
            for ep in service.get("endpoints", []):
                if ep.get("interface") == "public":
                    reg = ep.get("region_id", "")
                    url = ep.get("url", "").rstrip("/")
                    if reg.startswith("ru-") and url:
                        regions_map[reg] = url

    if not regions_map:
        raise RuntimeError(
            "Не найдено ни одного региона ru-* в каталоге Selectel."
        )

    return token, regions_map


def get_external_network_id(headers, network_url):
    url = f"{network_url}/v2.0/networks?router:external=true"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    networks = resp.json().get("networks", [])
    if not networks:
        return None
    for net in networks:
        if net.get("name") == "external-network":
            return net["id"]
    return networks[0]["id"]


def list_floating_ips(headers, network_url):
    url = f"{network_url}/v2.0/floatingips"
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json().get("floatingips", [])


def create_floating_ip(headers, network_url, ext_net_id):
    url = f"{network_url}/v2.0/floatingips"
    payload = {"floatingip": {"floating_network_id": ext_net_id}}
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json().get("floatingip", {})


def delete_floating_ip(headers, network_url, fip_id):
    url = f"{network_url}/v2.0/floatingips/{fip_id}"
    for _ in range(3):
        resp = requests.delete(url, headers=headers, timeout=15)
        if resp.status_code in (204, 404):
            print("[-] IP удален.")
            return True
        time.sleep(3)
    print(f"[!] Не удалось сразу удалить {fip_id}: код {resp.status_code}")
    return False


def cleanup_region_ips(headers, network_url, region_name):
    cleaned = 0
    try:
        for fip in list_floating_ips(headers, network_url):
            addr = fip.get("floating_ip_address", "")
            fip_id = fip.get("id", "")
            if not is_target_ip(addr) and not fip.get("port_id"):
                print(f"[*] [{region_name}] Чистим лишний IP: {addr}")
                if delete_floating_ip(headers, network_url, fip_id):
                    cleaned += 1
    except Exception as e:
        print(f"[!] [{region_name}] Ошибка при чистке: {e}")
    return cleaned


def main():
    print("Авторизация в Selectel и поиск зон ru-*...")
    token, regions_map = get_keystone_session()
    headers = {"X-Auth-Token": token, "Content-Type": "application/json"}

    print(
        f"Найдены российские зоны: {', '.join(sorted(regions_map.keys()))}"
    )

    active_regions = {}

    for reg, net_url in sorted(regions_map.items()):
        try:
            existing = list_floating_ips(headers, net_url)
            for fip in existing:
                ip_str = fip.get("floating_ip_address", "")
                if is_target_ip(ip_str):
                    msg = f"✅ Нужный IP уже есть на аккаунте (зона {reg}): {ip_str}. Прокрутка не требуется!"
                    print(f"\n{msg}")
                    send_tg_message(msg)
                    return

            cleanup_region_ips(headers, net_url, reg)

            ext_net_id = get_external_network_id(headers, net_url)
            if ext_net_id:
                active_regions[reg] = {"url": net_url, "ext_net_id": ext_net_id}
                print(f"[OK] Зона {reg} подключена.")
            else:
                print(f"[-] В зоне {reg} нет внешней сети, пропускаем.")
        except Exception as e:
            print(f"[-] Зона {reg} недоступна ({e}), пропускаем.")

    region_names = list(active_regions.keys())
    if not region_names:
        msg = "⚠️ Не удалось запустить прокрутку: нет ни одной доступной зоны ru-*."
        print(msg)
        send_tg_message(msg)
        return

    # === ОПОВЕЩЕНИЕ В TELEGRAM О НАЧАЛЕ ПРОКРУТКИ ===
    start_msg = (
        f"🚀 Начинаем прокрутку IP в Selectel!\n"
        f"📍 Активные зоны: {', '.join(region_names)}\n"
        f"🔄 План: до {MAX_ATTEMPTS_PER_RUN} попыток (по {ATTEMPTS_PER_ZONE} подряд в каждой зоне)."
    )
    print(f"\n{start_msg}")
    send_tg_message(start_msg)

    attempt = 1
    reg_index = 0
    zone_attempt = 0

    while attempt <= MAX_ATTEMPTS_PER_RUN and region_names:
        current_reg = region_names[reg_index % len(region_names)]
        reg_info = active_regions[current_reg]
        net_url = reg_info["url"]
        ext_net_id = reg_info["ext_net_id"]

        print(
            f"\n--- Общая попытка {attempt}/{MAX_ATTEMPTS_PER_RUN} | Зона {current_reg} ({zone_attempt + 1}/{ATTEMPTS_PER_ZONE}) ---"
        )
        try:
            new_fip = create_floating_ip(headers, net_url, ext_net_id)
            ip_address = new_fip.get("floating_ip_address", "")
            fip_id = new_fip.get("id", "")

            print(f"[{current_reg}] Выпал IP: {ip_address}")

            if is_target_ip(ip_address):
                msg = f"🎯 ПОБЕДА! В Selectel (зона {current_reg}) на попытке #{attempt} выбит нужный IP: {ip_address}"
                print(f"\n{msg}")
                send_tg_message(msg)
                for r_name in region_names:
                    cleanup_region_ips(
                        headers, active_regions[r_name]["url"], r_name
                    )
                return
            else:
                print("Не подходит. Удаляем...")
                time.sleep(2)
                delete_floating_ip(headers, net_url, fip_id)
                attempt += 1
                zone_attempt += 1

                if zone_attempt >= ATTEMPTS_PER_ZONE:
                    zone_attempt = 0
                    reg_index += 1
