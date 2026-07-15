const SYSTEM_PROMPT = `You are "Nancy Pelosi" - an extremely blunt, zero-excuses personal budget coach for
a hobby spending-tracker app. Your personality mirrors fitness inleucner "Soosh" so you like to randomly mention , buying K, gambling, The Ironman Triathlon, and you say brahand bro alot , do not
reference politics or anyone's real views/statements/biography - stay
entirely and only on the topic of this user's personal budget.

Tone: direct to the point of bluntness, no hedging, no coddling, no "I'm not
a financial advisor" disclaimers - the user knows exactly what this is. You
do not accept excuses - if they're overspending, say so plainly and tell
them what to actually do about it, framed as accountability rather than
cruelty. Short, punchy sentences over long explanations. Genuinely
enthusiastic and complimentary when they're actually doing well - the
bluntness cuts both ways, it's not just criticism. Act like a Bro

You are given the user's current pay-period spending data as JSON. Base every
answer strictly on those numbers - never invent figures. Use it to:
- Answer "how am I doing" with a short, direct status update
- Answer "can I buy X for $Y" by checking remaining headroom in the relevant
  category this pay period, and their checking account balance, then giving
  a clear yes/no/careful-here opinion with the reasoning
- Summarize the day if asked for a daily recap

Keep replies short - a few sentences, not an essay.`;

function corsHeaders(allowedOrigin) {
  return {
    "Access-Control-Allow-Origin": allowedOrigin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Chat-Secret",
  };
}

async function sendNtfyAlert(env, message) {
  if (!env.NTFY_TOPIC) return;
  try {
    await fetch(`https://ntfy.sh/${env.NTFY_TOPIC}`, {
      method: "POST",
      body: message,
      headers: { Title: "Nancy usage alert", Priority: "high" },
    });
  } catch {
    // best-effort, never let alerting break the request
  }
}

async function checkRateLimits(request, env) {
  const ip = request.headers.get("CF-Connecting-IP") || "unknown";
  const now = new Date();
  const minuteKey = `ip:${ip}:${now.toISOString().slice(0, 16)}`;
  const dayKey = `global:${now.toISOString().slice(0, 10)}`;

  const perIpLimit = parseInt(env.PER_IP_LIMIT_PER_MINUTE || "8", 10);
  const globalLimit = parseInt(env.GLOBAL_LIMIT_PER_DAY || "100", 10);

  const [ipCountStr, dayCountStr] = await Promise.all([
    env.RATE_LIMIT.get(minuteKey),
    env.RATE_LIMIT.get(dayKey),
  ]);
  const ipCount = parseInt(ipCountStr || "0", 10);
  const dayCount = parseInt(dayCountStr || "0", 10);

  if (ipCount >= perIpLimit) {
    return { blocked: true, reason: "per-IP rate limit" };
  }
  if (dayCount >= globalLimit) {
    return { blocked: true, reason: "global daily limit" };
  }

  await Promise.all([
    env.RATE_LIMIT.put(minuteKey, String(ipCount + 1), { expirationTtl: 90 }),
    env.RATE_LIMIT.put(dayKey, String(dayCount + 1), { expirationTtl: 172800 }),
  ]);

  // Alert once per crossing of a milestone, not on every request past it.
  const newDayCount = dayCount + 1;
  if (newDayCount === Math.floor(globalLimit * 0.5) || newDayCount === globalLimit) {
    await sendNtfyAlert(env, `Nancy chat has handled ${newDayCount}/${globalLimit} requests today.`);
  }

  return { blocked: false };
}

export default {
  async fetch(request, env) {
    const origin = request.headers.get("Origin") || "";
    const headers = corsHeaders(env.ALLOWED_ORIGIN);

    if (request.method === "OPTIONS") {
      return new Response(null, { headers });
    }

    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405, headers });
    }

    if (origin !== env.ALLOWED_ORIGIN) {
      return new Response("Forbidden", { status: 403, headers });
    }

    const providedSecret = request.headers.get("X-Chat-Secret") || "";
    if (providedSecret !== env.CHAT_SHARED_SECRET) {
      return new Response("Forbidden", { status: 403, headers });
    }

    const rateCheck = await checkRateLimits(request, env);
    if (rateCheck.blocked) {
      return new Response(
        JSON.stringify({ error: `Rate limit hit (${rateCheck.reason}). Try again shortly.` }),
        { status: 429, headers: { ...headers, "Content-Type": "application/json" } }
      );
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response("Bad request", { status: 400, headers });
    }

    const { question, context } = body;
    if (!question || typeof question !== "string") {
      return new Response("Missing question", { status: 400, headers });
    }

    const anthropicRes = await fetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-api-key": env.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
      },
      body: JSON.stringify({
        model: "claude-haiku-4-5",
        max_tokens: 400,
        system: SYSTEM_PROMPT,
        messages: [
          {
            role: "user",
            content: `Current budget data (JSON):\n${JSON.stringify(context)}\n\nQuestion: ${question}`,
          },
        ],
      }),
    });

    if (!anthropicRes.ok) {
      return new Response(
        JSON.stringify({ error: `Anthropic API error ${anthropicRes.status}` }),
        { status: 502, headers: { ...headers, "Content-Type": "application/json" } }
      );
    }

    const data = await anthropicRes.json();
    const reply = data.content?.[0]?.text || "Hmm, I've got nothing. Try again?";

    return new Response(JSON.stringify({ reply }), {
      headers: { ...headers, "Content-Type": "application/json" },
    });
  },
};
