/**
 * OpenWorld 免费 VPS 库存监控 - Cloudflare Worker 版
 * 支持 Cron 定时触发 (每分钟轮询) + HTTP 网页直接访问测试
 */

export default {
  // 1. 定时触发器 (Cron Triggers)
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(runCronLoop(env));
  },

  // 2. HTTP 访问触发器 (浏览器打开 worker.dev 即可直接查看实时状态)
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/test-tg") {
      // 访问 /test-tg 手动测试 Telegram 连通性
      const ok = await sendTelegram(
        env.TG_BOT_TOKEN,
        env.TG_CHAT_ID,
        `🤖 <b>【OpenWorld 监控测试】</b>\n\n来自 Cloudflare Worker 的 Telegram 连通性测试成功！\n⏰ 时间: ${getBeijingTime()}`
      );
      return new Response(ok ? "Telegram 测试消息发送成功！" : "Telegram 发送失败，请检查 Token 和 Chat ID", {
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
    }

    // 默认直接执行一次库存检测并返回结果
    const result = await checkStockOnce(env);
    return new Response(JSON.stringify(result, null, 2), {
      headers: { "Content-Type": "application/json; charset=utf-8" }
    });
  }
};

/**
 * 定时任务循环执行（单次 Cron 触发跑 3 轮，每轮间隔约 18 秒，实现 1 分钟内 20 秒高频检测）
 */
async function runCronLoop(env) {
  const TOTAL_ROUNDS = 3;      // 每分钟内检测 3 次
  const INTERVAL_MS = 18000;   // 每次间隔 18 秒

  for (let i = 1; i <= TOTAL_ROUNDS; i++) {
    console.log(`[CF Worker] 第 ${i}/${TOTAL_ROUNDS} 轮检测开始...`);
    const res = await checkStockOnce(env);

    if (res.stock && res.stock > 0) {
      console.log(`🎉 发现 Free 套餐有库存: ${res.stock}，已推送 TG，结束本次轮询`);
      break; // 有货时推送并退出，防止短时间内重复轰炸
    }

    if (i < TOTAL_ROUNDS) {
      await sleep(INTERVAL_MS);
    }
  }
}

/**
 * 单次检测逻辑
 */
async function checkStockOnce(env) {
  const targetUrl = "https://openworld.eu.org/createvps";
  const rawCookie = env.OPENWORLD_COOKIE || "";
  const tgToken = env.TG_BOT_TOKEN || "";
  const tgChatId = env.TG_CHAT_ID || "";

  if (!rawCookie || !tgToken || !tgChatId) {
    return {
      success: false,
      error: "环境变量未配置完整，请在 Worker Settings -> Variables 中配置 OPENWORLD_COOKIE, TG_BOT_TOKEN, TG_CHAT_ID"
    };
  }

  const cookieHeader = parseCookieHeader(rawCookie);

  try {
    const response = await fetch(targetUrl, {
      method: "GET",
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Cookie": cookieHeader,
        "Referer": "https://openworld.eu.org/"
      },
      redirect: "follow"
    });

    const finalUrl = response.url;
    const htmlText = await response.text();

    // 1. 检查 Cookie 是否过期跳转登录页
    if (finalUrl.toLowerCase().includes("/login") || (htmlText.includes("login") && !htmlText.includes("plan-card"))) {
      console.log("⚠️ Session Cookie 已失效！");
      await sendTelegram(
        tgToken,
        tgChatId,
        `⚠️ <b>【OpenWorld 监控报警】Session Cookie 已失效！</b>\n\n检测到被重定向至登录页，请重新登录获取新的 <code>sessioncookie</code> 并更新 Cloudflare Worker 环境变量。\n\n⏰ 时间: ${getBeijingTime()}`
      );
      return { success: false, error: "Session Cookie expired" };
    }

    // 2. 解析套餐 JSON
    const plans = parsePlansFromHtml(htmlText);
    const freePlan = plans.find(p => String(p.name || "").trim().toLowerCase() === "free");

    if (!freePlan) {
      return {
        success: false,
        error: "未找到 Free 套餐，页面结构可能变动",
        parsedPlans: plans
      };
    }

    const stock = parseInt(freePlan.stock || 0, 10);
    console.log(`[${getBeijingTime()}] Free 套餐当前库存: ${stock}`);

    // 3. 有库存时发送 Telegram 抢购提醒
    if (stock > 0) {
      const msg = formatSuccessMessage(freePlan, targetUrl);
      await sendTelegram(tgToken, tgChatId, msg);
    }

    return {
      success: true,
      time: getBeijingTime(),
      freePlanName: freePlan.name,
      stock: stock,
      hasStock: stock > 0,
      allPlans: plans.map(p => ({ name: p.name, price: p.price, stock: p.stock }))
    };

  } catch (err) {
    console.error("请求或解析异常:", err);
    return { success: false, error: String(err) };
  }
}

/**
 * 智能解析 Cookie 输入为 HTTP 请求头格式
 */
function parseCookieHeader(raw) {
  raw = raw.trim();
  if ((raw.startsWith("[") && raw.endsWith("]")) || (raw.startsWith("{") && raw.endsWith("}"))) {
    try {
      const data = JSON.parse(raw);
      if (Array.isArray(data)) {
        const item = data.find(c => c.name === "sessioncookie");
        if (item) return `sessioncookie=${item.value}`;
        return data.map(c => `${c.name}=${c.value}`).join("; ");
      } else if (typeof data === "object") {
        return Object.entries(data).map(([k, v]) => `${k}=${v}`).join("; ");
      }
    } catch (e) {}
  }
  if (raw.includes("=")) return raw;
  return `sessioncookie=${raw}`;
}

/**
 * 从页面提取套餐 JSON 数据
 */
function parsePlansFromHtml(htmlText) {
  const plans = [];
  // 匹配 openDeployModal('{...}')
  const regex = /openDeployModal\(\s*['"](\{.*?\})['"]\s*\)/g;
  let match;
  while ((match = regex.exec(htmlText)) !== null) {
    try {
      let jsonStr = match[1]
        .replace(/&#34;/g, '"')
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'")
        .replace(/&apos;/g, "'")
        .replace(/&amp;/g, '&');
      const obj = JSON.parse(jsonStr);
      plans.push(obj);
    } catch (e) {
      console.error("解析 JSON 出错:", e);
    }
  }
  return plans;
}

/**
 * 格式化有库存时的 TG 通知消息
 */
function formatSuccessMessage(plan, targetUrl) {
  const name = plan.name || "Free";
  const stock = plan.stock || 0;
  const cpu = plan.cpu || 1;
  const ram = plan.ram || 512;
  const disk = plan.disk || 5120;
  const netmbps = plan.netmbps || 50;
  const bandwidth = plan.bandwidth_gb || 50;

  let locDisplay = "默认节点可用";
  if (Array.isArray(plan.locations) && plan.locations.length > 0) {
    const locs = plan.locations
      .filter(l => l.available)
      .map(l => `${l.flag || ""} ${l.name || ""}`.trim());
    if (locs.length > 0) locDisplay = locs.join(", ");
  }

  return `🎉 <b>【发现 OpenWorld 免费 VPS 可用资源！】</b>\n\n` +
         `📦 <b>套餐名称</b>: ${name}\n` +
         `📊 <b>当前库存</b>: <b>${stock} 台</b>\n` +
         `💰 <b>套餐资费</b>: 免费 (0.00 / mo)\n\n` +
         `⚙️ <b>硬件配置</b>:\n` +
         `• 核心: ${cpu} vCore\n` +
         `• 内存: ${ram} MB\n` +
         `• 存储: ${disk} MB\n` +
         `• 带宽: ${netmbps} Mbps (${bandwidth} GB/周)\n` +
         `• 网络: IPv4 + IPv6\n` +
         `📍 <b>可用节点</b>: ${locDisplay}\n\n` +
         `⚡ <b>抢购地址</b>:\n` +
         `<a href="${targetUrl}">${targetUrl}</a>\n\n` +
         `⏰ <b>检测时间</b>: ${getBeijingTime()}\n` +
         `<i>💡 资源名额极少，手慢无，请尽快前往创建！</i>`;
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
    console.error("TG 发送失败:", e);
    return false;
  }
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
