#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NexioHost Free Discord Bot 库存监控脚本 (GitHub Actions / 本地无头浏览器版)
监控目标: https://billing.nexiohost.in/products/free-bot-hosting/free-discord-bot
参考 demo.py 的 Cloudflare Turnstile 绕过技术 (SeleniumBase UC 模式 + 虚拟显示屏 + uc_gui_click_captcha)
并在有库存时推送 Telegram 提醒。
"""

import os
import sys
import time
import re
import html
import platform
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime, timezone, timedelta
import requests
from bs4 import BeautifulSoup
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
RUN_ONCE = os.environ.get("RUN_ONCE", "true").strip().lower() in ("true", "1", "yes")  # 默认单次诊断模式
PROXY_SERVER = os.environ.get("PROXY_SERVER", "").strip()


# ==================== 辅助与环境配置 ====================
def is_linux() -> bool:
    """判断当前运行系统是否为 Linux (如 GitHub Actions Runner)"""
    return platform.system().lower() == "linux"


def setup_display():
    """
    在 Linux (GitHub Actions) 环境下启动 1920x1080 虚拟桌面。
    确保浏览器拥有标准 1080p 桌面分辨率，使 PyAutoGUI / uc_gui_click_captcha
    定位验证码复选框更加精准。
    """
    if is_linux() and not os.environ.get("DISPLAY"):
        try:
            from pyvirtualdisplay import Display
            display = Display(visible=False, size=(1920, 1080))
            display.start()
            os.environ["DISPLAY"] = display.new_display_var
            log(f"🖥️ 虚拟显示已启动 (1920x1080, DISPLAY={os.environ['DISPLAY']})")
            return display
        except Exception as e:
            log(f"⚠️ 虚拟显示启动失败 (将由 SeleniumBase xvfb 接管): {e}", "WARN")
    return None


def cn_time() -> str:
    """获取当前北京时间字符串"""
    return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str, level: str = "INFO"):
    """打印格式化日志"""
    print(f"[{cn_time()}] [{level}] {msg}", flush=True)


# ==================== Telegram 通知 ====================
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


def send_tg_photo(token: str, chat_id: str, photo_path: str, caption: str = "") -> bool:
    """发送本地截图到 Telegram"""
    if not token or not chat_id:
        log("未配置 TG_BOT_TOKEN 或 TG_CHAT_ID，跳过 Telegram 发送", "WARN")
        return False

    if not os.path.exists(photo_path):
        log(f"截图文件不存在: {photo_path}", "WARN")
        return False

    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    for attempt in range(1, 4):
        try:
            with open(photo_path, "rb") as f:
                files = {"photo": f}
                data = {
                    "chat_id": chat_id,
                    "caption": caption[:1024],
                    "parse_mode": "HTML"
                }
                resp = requests.post(url, files=files, data=data, timeout=30)
                if resp.status_code == 200:
                    log("✅ Telegram 页面截图发送成功！")
                    return True
                else:
                    log(f"⚠️ Telegram 截图发送失败 (尝试 {attempt}/3): HTTP {resp.status_code} - {resp.text}", "WARN")
        except Exception as e:
            log(f"⚠️ Telegram 截图发送异常 (尝试 {attempt}/3): {e}", "WARN")
        time.sleep(2)

    return False


# ==================== Cloudflare 盾处理 (参考 demo.py) ====================
def clear_browser_state(sb):
    """清除浏览器 Cookie、localStorage、sessionStorage (参考 demo.py)"""
    try:
        sb.execute_script('''
            try { window.localStorage.clear(); } catch(e) {}
            try { window.sessionStorage.clear(); } catch(e) {}
        ''')
    except Exception:
        pass
    try:
        sb.delete_all_cookies()
    except Exception:
        pass
    log("🧹 浏览器状态 (Cookie / 本地存储) 已清理")


def is_cloudflare_interstitial(sb) -> bool:
    """
    检查页面是否仍处于 Cloudflare 盾（5秒盾 / 人机验证 / Turnstile 拦截页）。
    参考 demo.py：
    1. 若已出现目标页面核心 DOM (wire:name="products.show" / h1 等)，则判定非盾。
    2. 检查 title / page_source 是否包含 CF 特征关键字。
    3. 检查 body 文本极短且含 challenges.cloudflare.com 的特征。
    """
    try:
        # 1. 优先检查是否已成功加载 Paymenter 商品容器
        has_product = sb.execute_script('''
            return !!(document.querySelector('[wire\\:name="products.show"]')
                   || (document.querySelector('h1') && document.querySelector('h1').innerText.toLowerCase().includes('discord'))
                   || document.querySelector('.nx-chip'));
        ''')
        if has_product:
            return False

        page_source = sb.get_page_source()
        title = sb.get_title().lower() if sb.get_title() else ""

        strong_indicators = [
            "Just a moment",
            "Verify you are human",
            "Checking your browser",
            "Checking if the site connection is secure",
            "Performing security verification",
            "Security Check",
            "challenges.cloudflare.com",
            "cf-mitigated",
        ]
        for indicator in strong_indicators:
            if indicator.lower() in page_source.lower():
                return True

        if "just a moment" in title or "attention required" in title or "security verification" in title:
            return True

        body_text_len = sb.execute_script('''
            return (document.body && document.body.innerText)
                ? document.body.innerText.trim().length : 0;
        ''')
        if body_text_len < 100 and "challenges.cloudflare.com" in page_source:
            return True

        current_url = sb.get_current_url()
        if "free-discord-bot" not in current_url:
            return True

        return False
    except Exception:
        return False


def wait_for_turnstile_success(sb, timeout: int = 20) -> bool:
    """等待 Turnstile 验证生成 Token 或标记为 solved (参考 demo.py)"""
    log("等待 Turnstile 凭据生成...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            success = sb.execute_script('''
                var resp = document.querySelector('input[name="cf-turnstile-response"]');
                if (resp && resp.value && resp.value.length > 20) return true;
                var grecap = document.querySelector('textarea[name="g-recaptcha-response"]');
                if (grecap && grecap.value && grecap.value.length > 20) return true;
                var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                if (iframe && iframe.getAttribute("data-state") === "solved") return true;
                return false;
            ''')
            if success:
                log("✅ Turnstile 凭据已生成或标记为 solved")
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def bypass_cloudflare_interstitial(sb, max_attempts: int = 3) -> bool:
    """
    参考 demo.py 的核心过盾逻辑：
    1. 最多尝试 max_attempts 次 uc_gui_click_captcha() 点击
    2. 每次点击后等待 6 秒给 Cloudflare 结算判定
    3. 若 3 次未过，使用 uc_open_with_reconnect(TARGET_URL, reconnect_time=10) 强制刷新
    """
    log("检测到 Cloudflare 整页挑战，执行参考 demo.py 绕过流程...")

    if not is_cloudflare_interstitial(sb):
        log("✅ 页面已不在 Cloudflare 挑战状态")
        return True

    for attempt in range(max_attempts):
        log(f"CF 绕过尝试 [{attempt + 1}/{max_attempts}]...")
        try:
            # 采用 demo.py 中证明有效的 uc_gui_click_captcha 方法
            sb.uc_gui_click_captcha()
            log("已调用 uc_gui_click_captcha()，等待 6s 验证结算...")
            time.sleep(6)

            if wait_for_turnstile_success(sb, timeout=4) or not is_cloudflare_interstitial(sb):
                time.sleep(2)
                if not is_cloudflare_interstitial(sb):
                    log("✅ Cloudflare 挑战已成功通过！")
                    return True
        except Exception as e:
            log(f"CF 绕过尝试 [{attempt + 1}] 异常: {e}", "WARN")

        time.sleep(3)

    log("尝试刷新页面重新加载 (reconnect_time=10s)...")
    try:
        sb.uc_open_with_reconnect(TARGET_URL, reconnect_time=10)
        time.sleep(5)
        if not is_cloudflare_interstitial(sb):
            log("✅ 刷新重连后 Cloudflare 挑战已通过！")
            return True

        log("刷新后仍有挑战，尝试最后一次点击...")
        try:
            sb.uc_gui_click_captcha()
            time.sleep(6)
            if not is_cloudflare_interstitial(sb):
                log("✅ 刷新并再次点击后通过 Cloudflare 挑战！")
                return True
        except Exception:
            pass
    except Exception as e:
        log(f"重新加载页面异常: {e}", "WARN")

    return False


def handle_initial_page(sb) -> bool:
    """初始页面加载与过盾 (参考 demo.py handle_initial_page)"""
    clear_browser_state(sb)

    log(f"正在访问目标地址: {TARGET_URL} (reconnect_time=8s)...")
    sb.uc_open_with_reconnect(TARGET_URL, reconnect_time=8)
    time.sleep(4)

    current_url = sb.get_current_url()
    log(f"当前页面 URL: {current_url}")

    if is_cloudflare_interstitial(sb):
        log("检测到 Cloudflare 整页挑战，启动过盾流程...")
        if not bypass_cloudflare_interstitial(sb, max_attempts=3):
            return False

    time.sleep(2)
    return not is_cloudflare_interstitial(sb)


# ==================== Paymenter DOM 解析 ====================
def parse_paymenter_stock(html_content: str) -> Dict[str, Any]:
    """针对 Paymenter 系统解析商品库存状态"""
    soup = BeautifulSoup(html_content, "html.parser")

    # 1. 定位 Livewire products.show 核心容器
    container = soup.find("div", attrs={"wire:name": "products.show"})
    if not container:
        h1_elem = soup.find("h1")
        container = h1_elem.parent if (h1_elem and h1_elem.parent) else soup

    container_text = container.get_text(" ", strip=True).lower()

    # 2. 提取商品标题与价格
    title = "Free Discord Bot"
    h1_elem = container.find("h1")
    if h1_elem:
        clean_title = h1_elem.get_text(strip=True)
        if clean_title:
            title = clean_title

    price = "Free"
    price_elem = container.find("span", class_=lambda c: c and "text-primary" in c)
    if price_elem and price_elem.get_text(strip=True):
        price = price_elem.get_text(strip=True)

    # 3. 提取规格配置参数
    specs = []
    article = container.find("article")
    if article:
        specs = [li.get_text(strip=True) for li in article.find_all("li") if li.get_text(strip=True)]

    # 4. 判断缺货提示 (检查 nx-chip 标签与文本)
    is_out_of_stock = False
    chip = container.find(class_=lambda c: c and "nx-chip" in c)
    if chip and "out of stock" in chip.get_text().lower():
        is_out_of_stock = True
    elif "product" in container_text and "is out of stock" in container_text:
        is_out_of_stock = True

    # 5. 检查具体剩余库存数量
    stock_count = None
    count_match = re.search(r"(\d+)\s*(?:in stock|available|units left)", container_text)
    if count_match:
        stock_count = int(count_match.group(1))

    # 6. 检查商品下单按钮或配置表单
    action_elements = container.find_all(["button", "a", "form"])
    has_order_action = any(
        any(w in el.get_text().lower() for w in ["order", "checkout", "continue", "buy", "select", "add to cart"]) or
        (el.has_attr("href") and "checkout" in el["href"].lower()) or
        (el.has_attr("action") and "checkout" in el["action"].lower())
        for el in action_elements
    )

    # 7. 综合判定库存
    has_stock = False
    if is_out_of_stock:
        has_stock = False
        status_desc = "缺货 (页面明确显示 Product is out of stock)"
    elif stock_count is not None and stock_count > 0:
        has_stock = True
        status_desc = f"有库存 (剩余 {stock_count} 个)"
    elif not is_out_of_stock and (has_order_action or "in stock" in container_text):
        has_stock = True
        status_desc = "有库存 (可立即下单)"
    else:
        has_stock = False
        status_desc = "未检测到下单表单，默认缺货"

    return {
        "title": title,
        "price": price,
        "specs": specs,
        "has_stock": has_stock,
        "stock_count": stock_count,
        "status_desc": status_desc
    }


def format_alert_message(stock_info: Dict[str, Any], target_url: str) -> str:
    """格式化有库存时的 TG 抢购提醒"""
    title = stock_info.get("title", "Free Discord Bot")
    desc = stock_info.get("status_desc", "有货！")
    price = stock_info.get("price", "Free")
    specs = stock_info.get("specs", [])
    count = stock_info.get("stock_count")
    stock_display = f"{count} 个" if count is not None else "有货 (可立即下单)"

    specs_text = ""
    if specs:
        specs_lines = "\n".join([f"• {html.escape(s)}" for s in specs])
        specs_text = f"⚙️ <b>配置参数</b>:\n{specs_lines}\n\n"

    return (
        f"🎉 <b>【发现 NexioHost Free Discord Bot 可用库存！】</b>\n\n"
        f"📦 <b>套餐名称</b>: {html.escape(title)}\n"
        f"📊 <b>当前库存</b>: <b>{stock_display}</b>\n"
        f"💰 <b>套餐资费</b>: {html.escape(price)}\n"
        f"📝 <b>状态详情</b>: {html.escape(desc)}\n\n"
        f"{specs_text}"
        f"⚡ <b>抢购地址</b>:\n"
        f"<a href=\"{target_url}\">{target_url}</a>\n\n"
        f"⏰ <b>检测时间</b>: {cn_time()}\n"
        f"<i>💡 免费 Bot 资源极速缺货，请尽快前往抢购！</i>"
    )


# ==================== 主监控流程 ====================
def run_monitor():
    """主监控循环"""
    mode_text = "单次诊断测试模式 (RUN_ONCE=True)" if RUN_ONCE else f"循环监控模式 (持续 {MAX_RUN_SECONDS}s)"
    log("=========================================================")
    log(f"NexioHost Free Discord Bot 监控启动 - [{mode_text}]")
    log(f"目标地址: {TARGET_URL}")
    log("=========================================================")

    # 1. 在 Linux 环境下启动 1920x1080 虚拟桌面 (demo.py 关键方案)
    display = setup_display()

    # 2. 构建与 demo.py 一致的 SeleniumBase 参数
    sb_kwargs = dict(
        uc=True,
        test=True,
        locale="en",
        headed=not is_linux(),
        user_data_dir=None,
        chromium_arg="--disable-blink-features=AutomationControlled",
    )
    if PROXY_SERVER:
        sb_kwargs["proxy"] = PROXY_SERVER

    # 若虚拟桌面未通过 pyvirtualdisplay 启动，则回退启用 SB 内置 xvfb
    if is_linux() and not os.environ.get("DISPLAY"):
        sb_kwargs["xvfb"] = True
        sb_kwargs["xvfb_metrics"] = "1920,1080"

    start_time = time.time()
    round_count = 0

    try:
        with SB(**sb_kwargs) as sb:
            while True:
                round_count += 1
                elapsed = int(time.time() - start_time)
                log(f"--- [第 {round_count} 轮检测] (已运行 {elapsed}s) ---")

                try:
                    if round_count == 1:
                        # 第一轮：全新清理状态并访问过盾
                        bypassed = handle_initial_page(sb)
                    else:
                        # 后续轮次：利用已获取的 cf_clearance 保持状态访问
                        sb.open(TARGET_URL)
                        time.sleep(3)
                        bypassed = True
                        if is_cloudflare_interstitial(sb):
                            log("检测到 Cloudflare 盾重现，重新执行绕过...")
                            bypassed = bypass_cloudflare_interstitial(sb, max_attempts=2)

                    if not bypassed:
                        log("本轮 Cloudflare 盾未通过", "WARN")

                        # 保存并发送拦截截图
                        screenshot_file = "cf_blocked.png"
                        try:
                            sb.save_screenshot(screenshot_file)
                            log(f"已保存 Cloudflare 拦截页面截图: {screenshot_file}")
                            caption = (
                                f"⚠️ <b>【NexioHost 监控 - Cloudflare 盾未通过】</b>\n\n"
                                f"📄 <b>当前标题</b>: {html.escape(sb.get_title())}\n"
                                f"🔗 <b>当前链接</b>: {html.escape(sb.get_current_url())}\n"
                                f"⏰ <b>检测时间</b>: {cn_time()}\n\n"
                                f"<i>提示: 脚本已尝试 uc_gui_click_captcha 过盾未果，请查阅截图排查。</i>"
                            )
                            send_tg_photo(TG_BOT_TOKEN, TG_CHAT_ID, screenshot_file, caption)
                        except Exception as err:
                            log(f"保存截图或发送 TG 异常: {err}", "WARN")

                        if RUN_ONCE:
                            print("\n" + "=" * 60)
                            print("⚠️ 【Cloudflare 盾检测诊断未通过】")
                            print("=" * 60)
                            print(f"当前页面标题: {sb.get_title()}")
                            print(f"当前页面 URL : {sb.get_current_url()}")
                            print("已将当前屏幕截图推送至 Telegram，请查阅。")
                            print("=" * 60 + "\n")
                            return

                        time.sleep(LOOP_INTERVAL)
                        continue

                    # 3. 成功到达商品页面，提取 HTML 解析库存
                    html_content = sb.get_page_source()
                    stock_info = parse_paymenter_stock(html_content)

                    # 在 Actions 控制台打印醒目的格式化诊断报告
                    stock_badge = "🎉 有货 (In stock)！" if stock_info["has_stock"] else "❌ 缺货 (Out of stock)"
                    print("\n" + "=" * 60)
                    print("📊 【NexioHost 商品监控状态报告】")
                    print("=" * 60)
                    print(f"📦 商品名称 : {stock_info['title']}")
                    print(f"💰 套餐价格 : {stock_info['price']}")
                    print(f"📈 库存状态 : {stock_badge}")
                    print(f"📝 状态说明 : {stock_info['status_desc']}")
                    if stock_info["stock_count"] is not None:
                        print(f"🔢 剩余数量 : {stock_info['stock_count']} 个")
                    if stock_info["specs"]:
                        print("⚙️ 硬件规格 :")
                        for spec in stock_info["specs"]:
                            print(f"   • {spec}")
                    print(f"🔗 监控地址 : {TARGET_URL}")
                    print(f"⏰ 检测时间 : {cn_time()}")
                    print("=" * 60 + "\n")

                    # 发现有库存时推送提醒并退出
                    if stock_info["has_stock"]:
                        log("🎉🎉🎉 检测到有可用库存！正在推送 Telegram 提醒...", "SUCCESS")
                        msg = format_alert_message(stock_info, TARGET_URL)
                        send_tg_message(TG_BOT_TOKEN, TG_CHAT_ID, msg)
                        screenshot_file = "in_stock.png"
                        try:
                            sb.save_screenshot(screenshot_file)
                            send_tg_photo(TG_BOT_TOKEN, TG_CHAT_ID, screenshot_file, "🎉 <b>【有库存页面截图】</b>")
                        except Exception:
                            pass
                        log("抢购提醒已发送，任务提前退出。")
                        return

                    # 单次模式：截取成功访问的商品页面推送到 TG 并退出
                    if RUN_ONCE:
                        screenshot_file = "product_page.png"
                        try:
                            sb.save_screenshot(screenshot_file)
                            log(f"已保存商品页面截图: {screenshot_file}")
                            caption = (
                                f"📊 <b>【NexioHost 商品页面诊断截图 - 成功突破 CF 盾】</b>\n\n"
                                f"📦 <b>商品名称</b>: {html.escape(stock_info['title'])}\n"
                                f"📈 <b>库存状态</b>: {stock_badge}\n"
                                f"📝 <b>状态详情</b>: {html.escape(stock_info['status_desc'])}\n"
                                f"⏰ <b>检测时间</b>: {cn_time()}"
                            )
                            send_tg_photo(TG_BOT_TOKEN, TG_CHAT_ID, screenshot_file, caption)
                        except Exception as err:
                            log(f"保存截图或发送 TG 异常: {err}", "WARN")

                        log("✅ 单次检测成功完成！成功突破 CF 盾并正常解析库存状态。")
                        return

                except Exception as e:
                    log(f"本轮检测发生异常: {e}", "ERROR")
                    if RUN_ONCE:
                        return

                if time.time() - start_time >= MAX_RUN_SECONDS:
                    log(f"已达到单次运行最大时长 ({MAX_RUN_SECONDS}s)，退出任务。")
                    break

                time.sleep(LOOP_INTERVAL)

    finally:
        if display:
            try:
                display.stop()
                log("虚拟显示已停止")
            except Exception:
                pass


if __name__ == "__main__":
    run_monitor()
