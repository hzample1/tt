#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NexioHost Free Discord Bot 库存监控脚本 (GitHub Actions / 本地无头浏览器版)
监控目标: https://billing.nexiohost.in/products/free-bot-hosting/free-discord-bot
通过 SeleniumBase UC 模式自动解决 Cloudflare Turnstile 验证盾，并在有库存时推送 Telegram 提醒。
"""

import os
import sys
import time
import re
import html
from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta
import requests
from seleniumbase import SB

# 控制台编码保护 (Windows)
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ==================== 基础配置 ====================
TARGET_URL = "https://billing.nexiohost.in/products/free-bot-hosting/free-discord-bot"
CN_TZ = timezone(timedelta(hours=8))
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "20"))        # 单次检测间隔（秒）
MAX_RUN_SECONDS = int(os.environ.get("MAX_RUN_SECONDS", "3540"))  # 单次 Action 运行时间（约 59 分钟）
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()


def cn_time() -> str:
    """获取当前北京时间字符串"""
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, level: str = "INFO"):
    """打印格式化日志"""
    print(f"[{cn_time()}] [{level}] {msg}", flush=True)


def send_tg_message(token: str, chat_id: str, text: str) -> bool:
    """发送 Telegram 富文本 HTML 消息"""
    if not token or not chat_id:
        log("未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 推送", "WARN")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }

    for attempt in range(1, 4):
        try:
            resp = requests.post(url, json=payload, timeout=20)
            if resp.status_code == 200:
                log("✅ Telegram 通知发送成功！")
                return True
            else:
                log(f"⚠️ Telegram 发送失败 (尝试 {attempt}/3): HTTP {resp.status_code} - {resp.text}", "WARN")
        except Exception as e:
            log(f"⚠️ Telegram 发送异常 (尝试 {attempt}/3): {e}", "WARN")
        time.sleep(2)

    return False


def is_turnstile_token_ready(sb) -> bool:
    """检查是否已经生成有效的 Turnstile Token"""
    try:
        return sb.execute_script('''
            var el = document.querySelector('input[name="cf-turnstile-response"]');
            return !!(el && el.value && el.value.length > 20);
        ''')
    except Exception:
        return False


def is_cf_page(sb) -> bool:
    """判断当前页面是否仍停留在 Cloudflare 5秒盾/人机验证页"""
    try:
        title = sb.get_title().lower()
        if "just a moment" in title or "请稍候" in title:
            return True
        source = sb.get_page_source().lower()
        if "cf-mitigated" in source or "id=\"challenge-error-text\"" in source:
            return True
        if "cf-turnstile-response" in source and ("security check" in source or "安全验证" in source or "verify you are human" in source):
            return True
    except Exception:
        pass
    return False


def bypass_cloudflare_challenge(sb, timeout: int = 60) -> bool:
    """
    等待并解决 Cloudflare Turnstile 验证盾
    """
    log("正在检测 Cloudflare 盾状态...")
    start = time.time()
    last_click = 0

    while time.time() - start < timeout:
        title = sb.get_title()
        current_url = sb.get_current_url()

        # 1. 检查是否已经通过盾
        if "just a moment" not in title.lower() and "请稍候" not in title and not is_cf_page(sb):
            log(f"✅ Cloudflare 盾已顺利通过！当前页面标题: {title}")
            return True

        # 2. 检查是否已生成 Token
        if is_turnstile_token_ready(sb):
            log("Turnstile 凭据已生成，等待页面跳转...")
            time.sleep(2)
            if not is_cf_page(sb):
                log("✅ 页面已完成跳转")
                return True

        # 3. 尝试触发点击
        now = time.time()
        if now - last_click > 4:
            last_click = now
            try:
                sb.uc_gui_click_cf()
                log("已尝试触发 uc_gui_click_cf")
            except Exception:
                try:
                    sb.uc_gui_click_captcha()
                    log("已尝试触发 uc_gui_click_captcha")
                except Exception as e:
                    log(f"点击验证框出现微异常 (忽略继续轮询): {e}", "DEBUG")

        time.sleep(1.5)

    final_pass = not is_cf_page(sb)
    if final_pass:
        log("✅ 最终确认已脱离 Cloudflare 盾")
        return True

    log("⚠️ 解决 Cloudflare 盾超时，未能加载真实页面", "WARN")
    return False


def parse_paymenter_stock(html_content: str) -> Dict[str, Any]:
    """
    解析 Paymenter 系统的商品卡片与购买可用性
    """
    lower = html_content.lower()

    # 1. 提取商品标题
    title = "Free Discord Bot"
    title_match = re.search(r"<h1[^>]*>(.*?)</h1>", html_content, re.IGNORECASE | re.DOTALL)
    if title_match:
        clean_title = re.sub(r"<[^>]+>", "", title_match.group(1)).strip()
        if clean_title:
            title = clean_title

    # 2. 缺货判定关键词
    out_of_stock_keywords = [
        "out of stock", "sold out", "0 in stock",
        "currently unavailable", "unavailable", "0 left", "no stock"
    ]
    is_out_of_stock = any(kw in lower for kw in out_of_stock_keywords)

    # 3. 提取剩余库存数字 (如 5 in stock / 10 available)
    stock_count = None
    count_match = re.search(r"(\d+)\s*(?:in stock|available|units left|left in stock)", lower)
    if count_match:
        stock_count = int(count_match.group(1))

    # 4. 检查下单按钮是否存在
    has_disabled_btn = bool(re.search(r"<button[^>]*disabled[^>]*>.*?(?:order|checkout|continue|buy|cart|out of stock).*?</button>", html_content, re.IGNORECASE | re.DOTALL))
    has_active_btn = bool(re.search(r"<button(?![^>]*disabled)[^>]*>.*?(?:order|checkout|continue|buy now|add to cart|select).*?</button>", html_content, re.IGNORECASE | re.DOTALL)) or (
        bool(re.search(r'<a(?![^>]*disabled)[^>]*href="[^"]*checkout[^"]*"[^>]*>', html_content, re.IGNORECASE))
    )

    # 5. 综合判定
    has_stock = False
    status_desc = "缺货 (Out of stock)"

    if stock_count is not None:
        if stock_count > 0:
            has_stock = True
            status_desc = f"有库存 (剩余 {stock_count} 个)"
        else:
            has_stock = False
            status_desc = "缺货 (0 in stock)"
    elif is_out_of_stock or has_disabled_btn:
        has_stock = False
        status_desc = "缺货 (页面标记 Out of stock 或下单按钮已被禁用)"
    elif has_active_btn:
        has_stock = True
        status_desc = "有库存 (检测到可用下单/结算按钮)"
    elif not is_out_of_stock and ("in stock" in lower or "available" in lower):
        has_stock = True
        status_desc = "有库存 (页面标记 In stock)"
    else:
        has_stock = False
        status_desc = "未检测到明确下单按钮，默认缺货"

    return {
        "title": title,
        "has_stock": has_stock,
        "stock_count": stock_count,
        "status_desc": status_desc
    }


def format_alert_message(stock_info: Dict[str, Any], target_url: str) -> str:
    """格式化有库存时的 TG 抢购提醒"""
    title = stock_info.get("title", "Free Discord Bot")
    desc = stock_info.get("status_desc", "有货！")
    count = stock_info.get("stock_count")
    stock_display = f"{count} 个" if count is not None else "有货 (可立即下单)"

    return (
        f"🎉 <b>【发现 NexioHost Free Discord Bot 可用库存！】</b>\n\n"
        f"📦 <b>套餐名称</b>: {html.escape(title)}\n"
        f"📊 <b>当前库存</b>: <b>{stock_display}</b>\n"
        f"💰 <b>套餐资费</b>: 免费 ($0.00 / mo)\n"
        f"📝 <b>状态详情</b>: {html.escape(desc)}\n\n"
        f"⚡ <b>抢购地址</b>:\n"
        f"<a href=\"{target_url}\">{target_url}</a>\n\n"
        f"⏰ <b>检测时间</b>: {cn_time()}\n"
        f"<i>💡 免费 Bot 资源极速缺货，请尽快前往抢购！</i>"
    )


def run_monitor():
    """主监控循环"""
    log("=========================================")
    log("NexioHost Free Discord Bot 监控开始启动...")
    log(f"监控目标: {TARGET_URL}")
    log(f"单次最长运行: {MAX_RUN_SECONDS} 秒, 轮询间隔: {LOOP_INTERVAL} 秒")
    log("=========================================")

    # Linux/GitHub Actions 环境下启用虚拟显示屏 xvfb=True
    use_xvfb = sys.platform != "win32"

    start_time = time.time()
    round_count = 0

    with SB(uc=True, xvfb=use_xvfb) as sb:
        while time.time() - start_time < MAX_RUN_SECONDS:
            round_count += 1
            elapsed = int(time.time() - start_time)
            log(f"--- [第 {round_count} 轮检测] (已运行 {elapsed}s/{MAX_RUN_SECONDS}s) ---")

            try:
                # 打开或刷新页面
                sb.uc_open_with_reconnect(TARGET_URL, 4)

                # 处理 Cloudflare Turnstile 验证盾
                bypassed = bypass_cloudflare_challenge(sb, timeout=45)
                if not bypassed:
                    log("本轮过盾未完成，稍后重试...", "WARN")
                    time.sleep(LOOP_INTERVAL)
                    continue

                # 获取页面 HTML 解析库存
                html_content = sb.get_page_source()
                stock_info = parse_paymenter_stock(html_content)

                log(f"检测结果 -> 套餐: {stock_info['title']} | 有货: {stock_info['has_stock']} | 状态: {stock_info['status_desc']}")

                # 发现有库存时推送并安全退出（避免短时间内重复轰炸）
                if stock_info["has_stock"]:
                    log("🎉🎉🎉 检测到有库存可用！正在推送 Telegram 提醒...", "SUCCESS")
                    msg = format_alert_message(stock_info, TARGET_URL)
                    send_tg_message(TG_BOT_TOKEN, TG_CHAT_ID, msg)
                    log("已发送抢购提醒，任务提前退出。")
                    return

            except Exception as e:
                log(f"本轮检测发生异常: {e}", "ERROR")

            time.sleep(LOOP_INTERVAL)

    log("本轮 GitHub Actions 定时任务运行周期结束，等待下一次调度。")


if __name__ == "__main__":
    run_monitor()
