import asyncio
"""Render service monitor: alerts on service/deploy failures and includes recent log excerpts when available."""
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta

import httpx

log = logging.getLogger("project-assistant.monitor")
SECRET_PATTERNS = [
    re.compile(r"(?i)(token|api[_-]?key|password|secret|authorization)(\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/-]+=*"),
]

def redact(text: str) -> str:
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) + (m.group(2) if m.lastindex and m.lastindex >= 2 else " ") + "[REDACTED]") if m.lastindex else "[REDACTED]", text)
    return text[:7000]

async def render_get(http, api_key, path, params=None):
    response = await http.get("https://api.render.com/v1" + path,
                              headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                              params=params)
    response.raise_for_status()
    return response.json() if response.content else {}

async def fetch_logs(http, api_key, service_id):
    # Render log endpoints can vary by API access; do not fail the monitor if logs are unavailable.
    try:
        data = await render_get(http, api_key, "/logs",
                                {"resource": service_id, "limit": 40})
        if isinstance(data, list):
            return "\\n".join(str(x.get("message", x)) for x in data[-30:])
        if isinstance(data, dict):
            entries = data.get("logs", data.get("data", []))
            if isinstance(entries, list):
                return "\\n".join(str(x.get("message", x)) for x in entries[-30:])
            return str(data)
    except Exception as exc:
        log.warning("Could not fetch Render logs for %s: %s", service_id, exc)
    return "تعذر جلب السجلات آليًا من واجهة Render المتاحة. افتح Logs للخدمة في لوحة Render."

def explain_basic(text):
    t = text.lower()
    if "modulenotfounderror" in t or "no module named" in t:
        return "السبب المرجح: مكتبة Python مفقودة. أضفها إلى requirements.txt ثم أعد النشر."
    if "missing required environment" in t or "environment variable" in t or "api_key" in t:
        return "السبب المرجح: متغير بيئة مفقود أو غير صحيح. راجع Environment في Render دون مشاركة الأسرار."
    if "out of memory" in t or "oom" in t:
        return "السبب المرجح: نفاد الذاكرة. افحص استهلاك الموارد وخفّض الحمل أو استخدم خطة بذاكرة أكبر."
    if "timeout" in t or "timed out" in t:
        return "السبب المرجح: مهلة اتصال أو خدمة خارجية بطيئة. افحص الشبكة والمهلات وحالة المزود."
    if "syntaxerror" in t or "indentationerror" in t:
        return "السبب المرجح: خطأ صياغة أو مسافات بادئة في Python. راجع السطر المذكور في السجل."
    if "unauthorized" in t or "401" in t or "403" in t:
        return "السبب المرجح: صلاحية أو مفتاح وصول غير صحيح/منتهي. راجع المتغيرات والصلاحيات."
    return "لم يتحدد السبب يقينًا من حالة الخدمة وحدها. راجع السجلات أدناه؛ التحليل آلي وقد يكون السبب المرجح بحاجة إلى تحقق."

async def analyze_logs(ai_client, ai_model, logs):
    if not ai_client or not logs:
        return explain_basic(logs)
    try:
        response = await ai_client.chat.completions.create(
            model=ai_model, temperature=0.1, max_tokens=500,
            messages=[
                {"role": "system", "content": "أنت مهندس SRE. حلل سجلات الأعطال فقط. اذكر السبب المرجح والدليل والإصلاح المقترح. لا تطلب الأسرار. لا تنفذ أي تغيير. أجب بالعربية باختصار."},
                {"role": "user", "content": redact(logs[-6500:])}
            ])
        return response.choices[0].message.content or explain_basic(logs)
    except Exception:
        return explain_basic(logs)

async def monitor_loop(bot, service_ids, api_key, owner_id, interval_seconds=120,
                       ai_client=None, ai_model="", ai_base_url=""):
    previous = {}
    async with httpx.AsyncClient(timeout=20) as http:
        while True:
            for service_id in service_ids:
                try:
                    service = await render_get(http, api_key, f"/services/{service_id}")
                    deploys = await render_get(http, api_key, f"/services/{service_id}/deploys",
                                               {"limit": 1})
                    latest = deploys[0].get("deploy", deploys[0]) if isinstance(deploys, list) and deploys else {}
                    status = str(latest.get("status", "unknown")).lower()
                    suspended = service.get("suspended", False)
                    key = (str(suspended), status)
                    bad = bool(suspended) or status in {"failed", "canceled", "cancelled", "deactivated"}
                    was_bad = previous.get(service_id, (None, False))[1]
                    if bad and (previous.get(service_id, (None, None))[0] != key or not was_bad):
                        logs = await fetch_logs(http, api_key, service_id)
                        diagnosis = await analyze_logs(ai_client, ai_model, logs)
                        name = service.get("name", service_id)
                        message = (
                            f"⚠️ تنبيه مراقبة Render\\nالخدمة: {name}\\nالمعرّف: {service_id}\\n"
                            f"حالة آخر نشر: {status}\\nالإيقاف: {suspended}\\n\\n"
                            f"التفسير والحل المقترح:\\n{diagnosis[:2200]}\\n\\n"
                            f"السجلات المتاحة:\\n{redact(logs)[:2200]}"
                        )
                        await bot.send_message(chat_id=owner_id, text=message[:3900])
                    elif not bad and was_bad:
                        await bot.send_message(chat_id=owner_id,
                            text=f"✅ عادت خدمة Render إلى حالة غير متوقفة: {service.get('name', service_id)}\\nآخر نشر: {status}")
                    previous[service_id] = (key, bad)
                except Exception as exc:
                    log.exception("Monitoring failed for Render service %s", service_id)
                    key = ("monitor-error", type(exc).__name__)
                    if previous.get(service_id, (None, None))[0] != key:
                        try:
                            await bot.send_message(chat_id=owner_id,
                                text=f"⚠️ تعذر فحص خدمة Render {service_id}: {type(exc).__name__}: {str(exc)[:500]}\\nتحقق من RENDER_API_KEY ومعرّف الخدمة وصلاحيات API.")
                        except Exception:
                            log.exception("Could not send monitor error alert")
                        previous[service_id] = (key, True)
            await asyncio.sleep(interval_seconds)
