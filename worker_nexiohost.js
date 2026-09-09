/**
 * NexioHost Free Discord Bot 库存监控 - Cloudflare Worker 版
 * 监控目标: https://billing.nexiohost.in/products/free-bot-hosting/free-discord-bot
 * 
 * 特性:
 * 1. 支持 Cron 定时触发 (每分钟高频轮询) + HTTP 网页直接查看监控状态
 * 2. 支持 Cloudflare 盾态识别与双模式过盾 (FlareSolverr 代理 / 浏览器 Cookie 注入)
 * 3. 针对 Paymenter 账单系统进行精准库存解析 (支持识别 Out of stock / In stock / Order 按钮态)
 * 4. Telegram Bot 即时富文本推送
 */

const TARGET_URL = "https://billing.nexiohost.in/products/free-bot-hosting/free-discord-bot";

export default {
  // 1. 定时触发器 (Cron Triggers)
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(runCronLoop(env));
  },

  // 2. HTTP 访问触发器 (浏览器打开 worker.dev 即可直接查看实时状态)
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // /test-tg 手动测试 Telegram 连通性
    if (url.pathname === "/test-tg") {
      const ok = await sendTelegram(
        env.TG_BOT_TOKEN,
        env.TG_CHAT_ID,
        `🤖 <b>【NexioHost 监控测试】</b>\n\n来自 Cloudflare Worker 的 Telegram 连通性测试成功！\n⏰ 时间: ${getBeijingTime()}`
      );
      return new Response(ok ? "Telegram 测试消息发送成功！" : "Telegram 发送失败，请检查 TG_BOT_TOKEN 和 TG_CHAT_ID 配置", {
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
    }

    // /help 查看使用指南与环境变量配置说明
    if (url.pathname === "/help") {
      return new Response(renderHelpText(), {
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
    }

    // 默认直接执行一次库存检测并返回 JSON 结果
    const result = await checkStockOnce(env);
    return new Response(JSON.stringify(result, null, 2), {
      headers: { "Content-Type": "application/json; charset=utf-8" }
    });
  }
};

/**
 * 定时任务循环执行（单次 Cron 触发跑 3 轮，每轮间隔约 18 秒，实现 1 分钟内 20 秒左右高频检测）
 */
async function runCronLoop(env) {
  const TOTAL_ROUNDS = 3;      // 每分钟内检测 3 次
  const INTERVAL_MS = 18000;   // 每次间隔 18 秒

  for (let i = 1; i <= TOTAL_ROUNDS; i++) {
    console.log(`[NexioHost Worker] 第 ${i}/${TOTAL_ROUNDS} 轮检测开始...`);
    const res = await checkStockOnce(env);

    if (res.hasStock) {
      console.log(`🎉 发现 NexioHost Free Bot 有库存！已推送 TG，结束本次轮询`);
      break; // 有货时已推送并退出，防止短时间内重复轰炸
    }

    if (i < TOTAL_ROUNDS) {
      await sleep(INTERVAL_MS);
    }
  }
}

/**
 * 单次检测核心逻辑
 */
async function checkStockOnce(env) {
  const tgToken = env.TG_BOT_TOKEN || "";
  const tgChatId = env.TG_CHAT_ID || "";
  const solverUrl = env.SOLVER_URL || "";
  const rawCookie = env.NEXIO_COOKIE || "";
  const userAgent = env.USER_AGENT || "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

  if (!tgToken || !tgChatId) {
    return {
      success: false,
      error: "环境变量未配置完整，请在 Worker Settings -> Variables 中配置 TG_BOT_TOKEN 和 TG_CHAT_ID (可选配置 SOLVER_URL 或 NEXIO_COOKIE)",
      help: "访问 /help 查看详细配置指南"
    };
  }

  try {
    let htmlText = "";
    let statusCode = 200;
    let usedMethod = "DIRECT";

    // 方案 1: 如果配置了 FlareSolverr 接口，优先使用 FlareSolverr 自动解盾
    if (solverUrl) {
      usedMethod = "FLARESOLVERR";
      const solverResult = await fetchWithFlareSolverr(solverUrl, TARGET_URL, rawCookie);
      if (!solverResult.success) {
        return {
          success: false,
          method: usedMethod,
          shieldStatus: "SOLVER_ERROR",
          error: `FlareSolverr 请求失败: ${solverResult.error}`,
          time: getBeijingTime()
        };
      }
      htmlText = solverResult.html;
      statusCode = solverResult.status;
    } else {
      // 方案 2: 直接请求，带上 Cookie (包含 cf_clearance) 和浏览器 User-Agent 伪装
      usedMethod = "DIRECT";
      const cookieHeader = parseCookieHeader(rawCookie);
      const response = await fetch(TARGET_URL, {
        method: "GET",
        headers: {
          "User-Agent": userAgent,
          "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
          "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
          "Cookie": cookieHeader,
          "Referer": "https://billing.nexiohost.in/",
          "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
          "sec-ch-ua-mobile": "?0",
          "sec-ch-ua-platform": '"Windows"',
          "sec-fetch-dest": "document",
          "sec-fetch-mode": "navigate",
          "sec-fetch-site": "same-origin",
          "sec-fetch-user": "?1",
          "upgrade-insecure-requests": "1"
        },
        redirect: "follow"
      });

      statusCode = response.status;
      const headers = response.headers;
      htmlText = await response.text();

      // 检查是否被 Cloudflare 盾拦截
      if (isCloudflareChallenge(statusCode, htmlText, headers)) {
        console.warn("[NexioHost Worker] 请求被 Cloudflare 盾拦截 (403 Challenge)");
        return {
          success: false,
          method: usedMethod,
          statusCode: statusCode,
          shieldStatus: "CHALLENGE_BLOCKED",
          error: "遭遇 Cloudflare 5秒盾/人机验证拦截 (403 Challenge)。建议配置 SOLVER_URL (FlareSolverr) 或在浏览器中通过验证后把包含 cf_clearance 的 Cookie 填入 NEXIO_COOKIE 环境变量。",
          time: getBeijingTime()
        };
      }
    }

    // 页面脱壳成功，开始解析库存
    const stockInfo = parsePaymenterStock(htmlText);

    console.log(`[${getBeijingTime()}] 检测完成 - 有库存: ${stockInfo.hasStock}, 状态描述: ${stockInfo.statusText}`);

    // 有库存时推送到 Telegram
    if (stockInfo.hasStock) {
      const message = formatTelegramAlert(stockInfo, TARGET_URL);
      await sendTelegram(tgToken, tgChatId, message);
    }

    return {
      success: true,
      method: usedMethod,
      statusCode: statusCode,
      shieldStatus: "PASSED",
      productName: stockInfo.title || "Free Discord Bot",
      hasStock: stockInfo.hasStock,
      stockCount: stockInfo.stockCount,
      statusDescription: stockInfo.statusText,
      price: stockInfo.price || "Free",
      time: getBeijingTime(),
      targetUrl: TARGET_URL
    };

  } catch (err) {
    console.error("[NexioHost Worker] 监控异常:", err);
    return {
      success: false,
      shieldStatus: "UNKNOWN",
      error: String(err && err.message ? err.message : err),
      time: getBeijingTime()
    };
  }
}

/**
 * 判断响应是否为 Cloudflare 验证盾 (Turnstile / Managed Challenge / 5s盾)
 */
function isCloudflareChallenge(status, body, headers) {
  if (status === 403) {
    const mitigated = headers ? (headers.get("cf-mitigated") || "") : "";
    if (mitigated.toLowerCase().includes("challenge")) return true;
  }
  const bodyLower = body.toLowerCase();
  if (bodyLower.includes("challenges.cloudflare.com") || 
      bodyLower.includes("cf-turnstile-response") || 
      bodyLower.includes("cf_chl_opt") ||
      bodyLower.includes("<title>just a moment...</title>") ||
      bodyLower.includes("<title>请稍候…</title>") ||
      bodyLower.includes("id=\"challenge-error-text\"")) {
    return true;
  }
  return false;
}

/**
 * 通过 FlareSolverr 穿盾代理抓取页面
 */
async function fetchWithFlareSolverr(solverUrl, targetUrl, rawCookie) {
  let endpoint = solverUrl.trim();
  if (!endpoint.endsWith("/v1")) {
    endpoint = endpoint.replace(/\/+$/, "") + "/v1";
  }

  const payload = {
    cmd: "request.get",
    url: targetUrl,
    maxTimeout: 60000
  };

  if (rawCookie) {
    const parsedCookies = parseCookiesToSolverFormat(rawCookie, "billing.nexiohost.in");
    if (parsedCookies.length > 0) {
      payload.cookies = parsedCookies;
    }
  }

  const res = await fetch(endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });

  if (!res.ok) {
    return { success: false, error: `FlareSolverr HTTP ${res.status} ${res.statusText}` };
  }

  const json = await res.json();
  if (json.status !== "ok" || !json.solution) {
    return { success: false, error: json.message || "FlareSolverr 未能成功穿盾" };
  }

  return {
    success: true,
    html: json.solution.response || "",
    status: json.solution.status || 200,
    cookies: json.solution.cookies
  };
}

/**
 * 解析 Paymenter 商品页面的库存状态
 */
function parsePaymenterStock(html) {
  const lower = html.toLowerCase();

  // 1. 提取商品标题
  let title = "Free Discord Bot";
  const titleMatch = html.match(/<h1[^>]*>([^<]+)<\/h1>/i) || html.match(/<title>([^<]+)<\/title>/i);
  if (titleMatch) {
    title = titleMatch[1].trim();
  }

  // 2. 提取价格
  let price = "Free";
  const priceMatch = html.match(/(\$\s*0(?:\.00)?|0\.00\s*(?:EUR|USD|\$|₹)|Free)/i);
  if (priceMatch) {
    price = priceMatch[1].trim();
  }

  // 3. 检查明确的缺货关键词
  const outOfStockKeywords = [
    "out of stock",
    "sold out",
    "0 in stock",
    "currently unavailable",
    "unavailable",
    "no stock",
    "0 left"
  ];
  const isOutOfStockText = outOfStockKeywords.some(kw => lower.includes(kw));

  // 4. 检查是否有明确的在售库存数字 (例如 "10 in stock", "Stock: 5", "5 Available")
  let stockCount = null;
  const countMatch = html.match(/(\d+)\s*(?:in stock|available|units left|left in stock)/i);
  if (countMatch) {
    stockCount = parseInt(countMatch[1], 10);
  }

  // 5. 检查下单按钮是否存在且可用
  // Paymenter 中有库存通常有 Order / Checkout / Continue / Select / Add to cart 且未 disabled
  const hasDisabledOrderButton = /<button[^>]*disabled[^>]*>(?:[^<]*(?:order|checkout|continue|buy|cart|out of stock)[^<]*)<\/button>/i.test(html);
  const hasActiveOrderButton = /<button(?![^>]*disabled)[^>]*>(?:[^<]*(?:order|checkout|continue|buy now|add to cart|select)[^<]*)<\/button>/i.test(html) ||
                               /<a(?![^>]*disabled)[^>]*href="[^"]*checkout[^"]*"[^>]*>/i.test(html);

  // 综合判断库存逻辑
  let hasStock = false;
  let statusText = "缺货 (Out of stock)";

  if (stockCount !== null) {
    if (stockCount > 0) {
      hasStock = true;
      statusText = `有库存 (剩余 ${stockCount} 个)`;
    } else {
      hasStock = false;
      statusText = "缺货 (0 in stock)";
    }
  } else if (isOutOfStockText || hasDisabledOrderButton) {
    hasStock = false;
    statusText = "缺货 (页面显示 Out of stock 或下单按钮已被禁用)";
  } else if (hasActiveOrderButton) {
    hasStock = true;
    statusText = "有库存 (存在可用下单按钮)";
  } else if (!isOutOfStockText && (lower.includes("in stock") || lower.includes("available"))) {
    hasStock = true;
    statusText = "有库存 (页面标记 In stock)";
  } else {
    // 兜底：若既无缺货也无明确按钮，保守标记为缺货并记录日志
    hasStock = false;
    statusText = "未检测到明确可下单按钮，默认缺货";
  }

  return {
    title,
    price,
    hasStock,
    stockCount,
    statusText
  };
}

/**
 * 格式化有库存时的 TG 通知消息
 */
function formatTelegramAlert(stockInfo, targetUrl) {
  const stockDisplay = stockInfo.stockCount !== null ? `${stockInfo.stockCount} 个` : "有货 (可立即下单)";

  return `🎉 <b>【发现 NexioHost Free Discord Bot 可用库存！】</b>\n\n` +
         `📦 <b>套餐名称</b>: ${escapeHtml(stockInfo.title)}\n` +
         `📊 <b>当前库存</b>: <b>${stockDisplay}</b>\n` +
         `💰 <b>套餐资费</b>: ${escapeHtml(stockInfo.price)}\n` +
         `📝 <b>状态详情</b>: ${escapeHtml(stockInfo.statusText)}\n\n` +
         `⚡ <b>抢购地址</b>:\n` +
         `<a href="${targetUrl}">${targetUrl}</a>\n\n` +
         `⏰ <b>检测时间</b>: ${getBeijingTime()}\n` +
         `<i>💡 免费 Bot 资源极易秒空，请点击上方链接火速前往注册领取！</i>`;
}

/**
 * 发送 Telegram 消息
 */
async function sendTelegram(token, chatId, text) {
  if (!token || !chatId) return false;
  const url = `https://api.telegram.org/bot${token}/sendMessage`;
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        chat_id: chatId,
        text: text,
        parse_mode: "HTML",
        disable_web_page_preview: false
      })
    });
    return resp.ok;
  } catch (e) {
    console.error("[NexioHost Worker] TG 发送失败:", e);
    return false;
  }
}

/**
 * Cookie 字符串转标准 Header
 */
function parseCookieHeader(raw) {
  if (!raw) return "";
  raw = raw.trim();
  if ((raw.startsWith("[") && raw.endsWith("]")) || (raw.startsWith("{") && raw.endsWith("}"))) {
    try {
      const data = JSON.parse(raw);
      if (Array.isArray(data)) {
        return data.map(c => `${c.name}=${c.value}`).join("; ");
      } else if (typeof data === "object") {
        return Object.entries(data).map(([k, v]) => `${k}=${v}`).join("; ");
      }
    } catch (e) {}
  }
  return raw;
}

/**
 * Cookie 字符串转 FlareSolverr 格式
 */
function parseCookiesToSolverFormat(raw, domain) {
  const result = [];
  if (!raw) return result;
  raw = raw.trim();

  if (raw.startsWith("[") && raw.endsWith("]")) {
    try {
      const arr = JSON.parse(raw);
      if (Array.isArray(arr)) {
        return arr.map(c => ({
          name: c.name,
          value: c.value,
          domain: c.domain || domain
        }));
      }
    } catch (e) {}
  }

  const parts = raw.split(";");
  for (const part of parts) {
    if (part.includes("=")) {
      const [k, v] = part.trim().split("=", 2);
      if (k && v) {
        result.push({
          name: k.trim(),
          value: v.trim(),
          domain: domain
        });
      }
    }
  }
  return result;
}

/**
 * 帮助文档输出
 */
function renderHelpText() {
  return `=== NexioHost Free Discord Bot 监控 Worker 使用说明 ===

1. 环境变量配置 (Worker -> Settings -> Variables):
   - TG_BOT_TOKEN: 必填，Telegram 机器人 Token (如 123456:ABC-DEF...)
   - TG_CHAT_ID: 必填，Telegram 接收提醒的用户或群组 ID (如 123456789)
   - SOLVER_URL: 选填 (推荐)，FlareSolverr 穿盾接口 (如 http://your-vps-ip:8191/v1)
   - NEXIO_COOKIE: 选填，浏览器抓取的 Cookie 字符串 (需包含 cf_clearance 和 session)
   - USER_AGENT: 选填，与 Cookie 配套的浏览器 UA 字符串

2. 路径说明:
   - / : 手动触发单次检测，以 JSON 格式输出当前库存及过盾状态
   - /test-tg : 发送一条测试消息到绑定的 Telegram
   - /help : 查看本帮助

3. 定时触发 (Cron Triggers):
   - 在 Worker 的 Triggers 中添加 Cron: "* * * * *" (每分钟触发)
   - 单次触发内自动轮询 3 次 (每 18 秒一次)，实现高频实时监控。
`;
}

/**
 * HTML 转义
 */
function escapeHtml(text) {
  if (!text) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/**
 * 获取当前北京时间 (UTC+8)
 */
function getBeijingTime() {
  return new Date().toLocaleString("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false
  });
}

/**
 * 毫秒休眠
 */
function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
