#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenWorld Free VPS 库存监控脚本
监控目标: https://openworld.eu.org/createvps
当 Free 套餐有库存时，通过 Telegram Bot 发送提醒。
"""

import os
import sys
import json
import re
import html
import time
from typing import List, Dict, Optional, Tuple, Any
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup

# 控制台编码保护 (Windows)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== 基础配置 ====================
TARGET_URL = "https://openworld.eu.org/createvps"
CN_TZ = timezone(timedelta(hours=8))
REQUEST_TIMEOUT = 25
MAX_RETRIES = 3


def cn_time() -> str:
    """获取当前北京时间字符串"""
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str):
    """打印带时间戳的日志"""
    print(f"[{cn_time()}] {msg}")


def parse_cookie_input(raw_cookie: Optional[str]) -> Dict[str, str]:
    """
    智能解析 Cookie 输入:
    1. 支持 JSON 数组: [{"name": "sessioncookie", "value": "..."}, ...]
    2. 支持 JSON 对象: {"sessioncookie": "..."}
    3. 支持 Cookie 字符串: sessioncookie=...; other=...
    4. 支持纯 Token 值: 直接作为 sessioncookie 的值
    """
    if not raw_cookie:
        return {}

    raw = raw_cookie.strip()

    # 尝试 JSON 解析
    if (raw.startswith("[") and raw.endswith("]")) or (raw.startswith("{") and raw.endswith("}")):
        try:
            data = json.loads(raw)
            cookies = {}
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "name" in item and "value" in item:
                        cookies[item["name"]] = item["value"]
            elif isinstance(data, dict):
                cookies = {str(k): str(v) for k, v in data.items()}
            if cookies:
                return cookies
        except Exception:
            pass

    # 尝试 key=value; key2=value2 格式
    if "=" in raw:
        cookies = {}
        parts = raw.split(";")
        for part in parts:
            if "=" in part:
                k, v = part.strip().split("=", 1)
                cookies[k.strip()] = v.strip()
        if cookies:
            return cookies

    # 否则直接视为 sessioncookie 的值
    return {"sessioncookie": raw}


def send_tg_message(token: str, chat_id: str, text: str, proxy: Optional[str] = None) -> bool:
    """发送 Telegram 消息"""
    if not token or not chat_id:
        log("❌ 未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过发送")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }

    proxies = {"http": proxy, "https": proxy} if proxy else None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=20, proxies=proxies)
            if resp.status_code == 200:
                log("✅ Telegram 通知发送成功！")
                return True
            else:
                log(f"⚠️ Telegram 发送失败 (尝试 {attempt}/{MAX_RETRIES}): HTTP {resp.status_code} - {resp.text}")
        except Exception as e:
            log(f"⚠️ Telegram 发送异常 (尝试 {attempt}/{MAX_RETRIES}): {e}")
        time.sleep(2)

    return False


def fetch_page(cookies: Dict[str, str], proxy: Optional[str] = None) -> Tuple[int, str, str]:
    """
    抓取目标页面
    返回: (状态码, 最终URL, 页面HTML内容)
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Referer": "https://openworld.eu.org/",
    }

    session = requests.Session()
    session.cookies.update(cookies)
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
        log(f"已为请求挂载代理: {proxy}")

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log(f"正在请求 {TARGET_URL} (第 {attempt} 次)...")
            resp = session.get(TARGET_URL, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            return resp.status_code, resp.url, resp.text
        except Exception as e:
            log(f"⚠️ 请求异常 (尝试 {attempt}/{MAX_RETRIES}): {e}")
            if attempt < MAX_RETRIES:
                time.sleep(3)

    return 0, "", ""


def parse_plans(html_content: str) -> List[Dict[str, Any]]:
    """
    从页面解析出所有 VPS 套餐及其库存状态
    优先提取 openDeployModal 中的 JSON 数据，辅以 DOM 兜底解析
    """
    plans: List[Dict[str, Any]] = []

    # 1. 尝试从 openDeployModal('...') 中提取 JSON
    # 页面格式通常形如: onclick="openDeployModal('{...}')" 或 onclick="openDeployModal('{\&quot;...}')"
    # 使用正则表达式匹配
    modal_matches = re.findall(r"openDeployModal\(\s*'(\{.*?\})'\s*\)", html_content)
    if not modal_matches:
        modal_matches = re.findall(r'openDeployModal\(\s*"(\{.*?\})"\s*\)', html_content)

    for raw_json in modal_matches:
        try:
            # 反转义可能存在的 HTML 实体 (例如 &quot;)
            unescaped = html.unescape(raw_json)
            plan_obj = json.loads(unescaped)
            plans.append(plan_obj)
        except Exception as e:
            log(f"⚠️ 解析 openDeployModal JSON 异常: {e}")

    if plans:
        return plans

    # 2. DOM 兜底解析
    soup = BeautifulSoup(html_content, "html.parser")
    cards = soup.find_all(class_="plan-card")
    for card in cards:
        name_elem = card.find(class_="plan-name")
        price_elem = card.find(class_="plan-price")
        name = name_elem.get_text(strip=True) if name_elem else "Unknown"
        price_str = price_elem.get_text(strip=True) if price_elem else "0"

        # 提取规格
        specs = {}
        for spec_div in card.find_all(class_="plan-spec"):
            k_elem = spec_div.find(class_="k")
            v_elem = spec_div.find(class_="v")
            if k_elem and v_elem:
                specs[k_elem.get_text(strip=True).lower()] = v_elem.get_text(strip=True)

        stock_text = specs.get("stock", "")
        # 判断库存数值
        stock_num = 0
        if "out of stock" not in stock_text.lower():
            num_match = re.search(r"(\d+)", stock_text)
            if num_match:
                stock_num = int(num_match.group(1))
            else:
                stock_num = 1  # 非 out of stock 且无具体数字时默认视为有货

        plans.append({
            "name": name,
            "price": price_str,
            "stock": stock_num,
            "stock_text": stock_text,
            "raw_specs": specs
        })

    return plans


def format_success_message(plan: Dict[str, Any]) -> str:
    """格式化有库存时的 TG 通知消息"""
    name = plan.get("name", "Free")
    stock = plan.get("stock", 0)
    cpu = plan.get("cpu", 1)
    ram = plan.get("ram", 512)
    disk = plan.get("disk", 5120)
    netmbps = plan.get("netmbps", 50)
    bandwidth = plan.get("bandwidth_gb", 50)

    # 提取可用节点
    locations = plan.get("locations", [])
    loc_names = []
    if locations:
        for loc in locations:
            if isinstance(loc, dict):
                loc_name = loc.get("name", "")
                is_avail = loc.get("available", True)
                flag = loc.get("flag", "")
                if is_avail:
                    loc_names.append(f"{flag} {loc_name}".strip())
    loc_display = ", ".join(loc_names) if loc_names else "默认节点可用"

    msg = (
        f"🎉 <b>【发现 OpenWorld 免费 VPS 可用资源！】</b>\n\n"
        f"📦 <b>套餐名称</b>: {name}\n"
        f"📊 <b>当前库存</b>: <b>{stock} 台</b>\n"
        f"💰 <b>套餐资费</b>: 免费 (0.00 / mo)\n\n"
        f"⚙️ <b>硬件配置</b>:\n"
        f"• 核心: {cpu} vCore\n"
        f"• 内存: {ram} MB\n"
        f"• 存储: {disk} MB\n"
        f"• 带宽: {netmbps} Mbps ({bandwidth} GB/周)\n"
        f"• 网络: IPv4 + IPv6\n"
        f"📍 <b>可用节点</b>: {loc_display}\n\n"
        f"⚡ <b>抢购地址</b>:\n"
        f"<a href=\"{TARGET_URL}\">{TARGET_URL}</a>\n\n"
        f"⏰ <b>检测时间</b>: {cn_time()}\n"
        f"<i>💡 资源名额极少，手慢无，请尽快前往创建！</i>"
    )
    return msg


def main():
    log("==========================================")
    log("OpenWorld 免费 VPS 库存监控任务启动")
    log("==========================================")

    # 1. 获取配置（必须从环境变量/Secrets中读取）
    env_cookie = os.environ.get("OPENWORLD_COOKIE", "").strip()
    tg_token   = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    proxy_server = os.environ.get("PROXY_SERVER", "").strip() or None

    if not env_cookie:
        log("❌ 缺少必须的环境变量: OPENWORLD_COOKIE 未设置！请在 GitHub Secrets 或环境中配置。")
        sys.exit(1)

    if not tg_token or not tg_chat_id:
        log("❌ 缺少必须的环境变量: TG_BOT_TOKEN 或 TG_CHAT_ID 未设置！请在 GitHub Secrets 或环境中配置。")
        sys.exit(1)

    cookies = parse_cookie_input(env_cookie)
    if not cookies:
        log("❌ OPENWORLD_COOKIE 为空或格式无效，无法解析！")
        sys.exit(1)

    log(f"已加载 Cookie 键: {list(cookies.keys())}")
    if proxy_server:
        log(f"已配置自定义代理 PROXY_SERVER: {proxy_server}")

    # 2. 请求页面
    status_code, final_url, html_content = fetch_page(cookies, proxy=proxy_server)
    if status_code == 0 or not html_content:
        log("❌ 请求目标页面失败，终止运行")
        sys.exit(1)

    # 3. 登录有效性检测
    # 若跳转到 /login 或页面未授权
    if "/login" in final_url.lower() or ("login" in html_content.lower() and "plan-card" not in html_content):
        log("⚠️ 检测到重定向至登录页或未授权状态，Session Cookie 可能已过期！")
        notify_text = (
            f"⚠️ <b>【OpenWorld 监控报警】Session Cookie 已失效！</b>\n\n"
            f"监控检测到当前 Cookie 无法访问控制台（已重定向到登录页面）。\n"
            f"请重新登录 OpenWorld 获取新的 <code>sessioncookie</code>，并在 GitHub 仓库 Secrets 中更新 <code>OPENWORLD_COOKIE</code>。\n\n"
            f"⏰ 时间: {cn_time()}"
        )
        send_tg_message(tg_token, tg_chat_id, notify_text, proxy=proxy_server)
        sys.exit(1)

    log(f"页面请求成功 (HTTP {status_code})，开始解析套餐库存...")

    # 4. 套餐库存解析
    plans = parse_plans(html_content)
    if not plans:
        log("⚠️ 未在页面中找到任何套餐信息，请检查页面结构是否变动！")
        sys.exit(1)

    log(f"成功解析到 {len(plans)} 个套餐:")
    free_plan: Optional[Dict[str, Any]] = None

    for p in plans:
        name = str(p.get("name", ""))
        stock = p.get("stock", 0)
        price = p.get("price", "N/A")
        log(f"  - 套餐: {name:<12} | 价格: {str(price):<8} | 库存: {stock}")

        # 识别 Free 套餐
        if name.strip().lower() == "free":
            free_plan = p

    # 5. 结果判断与通知
    if not free_plan:
        log("⚠️ 未找到名为 'Free' 的套餐！")
        return

    free_stock = int(free_plan.get("stock", 0))
    log(f"👉 免费套餐 (Free) 当前库存: {free_stock}")

    if free_stock > 0:
        log(f"🎉 发现 Free 套餐有货！当前库存: {free_stock}，正在发送 Telegram 通知...")
        msg = format_success_message(free_plan)
        send_tg_message(tg_token, tg_chat_id, msg, proxy=proxy_server)
    else:
        log("💤 Free 套餐暂无可用资源 (库存为 0)，保持静默，不发送任何通知。")

    log("监控任务完成，正常退出。")


if __name__ == "__main__":
    main()
