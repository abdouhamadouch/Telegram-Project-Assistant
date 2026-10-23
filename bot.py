import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from typing import Optional

import httpx
from openai import AsyncOpenAI
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from monitor import monitor_loop

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("project-assistant")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1").strip()
AI_MODEL = os.getenv("AI_MODEL", "openai/gpt-oss-20b").strip()
GH_TOKEN = os.getenv("GH_TOKEN", "").strip()
GH_REPO = os.getenv("GH_REPO", "").strip()
RENDER_API_KEY = os.getenv("RENDER_API_KEY", "").strip()
RENDER_SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "").strip()
MONITOR_RENDER_SERVICE_IDS = [x.strip() for x in os.getenv("MONITOR_RENDER_SERVICE_IDS", "").split(",") if x.strip()]
MONITOR_INTERVAL_SECONDS = max(60, int(os.getenv("MONITOR_INTERVAL_SECONDS", "120")))
MONITOR_TASK = None
DB_PATH = os.getenv("DB_PATH", "/data/assistant.sqlite3")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if OWNER_ID <= 0:
    raise RuntimeError("Set OWNER_ID to your numeric Telegram user ID")

client = AsyncOpenAI(api_key=AI_API_KEY, base_url=AI_BASE_URL) if AI_API_KEY else None


def db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            note TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id)")


def is_owner(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == OWNER_ID)


def owner_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_owner(update):
            if update.effective_message:
                await update.effective_message.reply_text("هذا البوت خاص بصاحبه.")
            return
        return await func(update, context)
    return wrapped


def save_message(user_id: int, role: str, content: str):
    with db() as con:
        con.execute(
            "INSERT INTO messages(user_id, role, content, created_at) VALUES(?,?,?,?)",
            (user_id, role, content[:12000], datetime.now(timezone.utc).isoformat()),
        )
        # Bound storage and prompt growth.
        con.execute(
            "DELETE FROM messages WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM messages WHERE user_id=? ORDER BY id DESC LIMIT 80)",
            (user_id, user_id),
        )


def get_history(user_id: int, limit: int = 16):
    with db() as con:
        rows = con.execute(
            "SELECT role, content FROM messages WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]


def get_memories(user_id: int, limit: int = 30):
    with db() as con:
        rows = con.execute(
            "SELECT note FROM memories WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [r["note"] for r in reversed(rows)]


def add_memory(user_id: int, note: str):
    with db() as con:
        con.execute(
            "INSERT INTO memories(user_id,note,created_at) VALUES(?,?,?)",
            (user_id, note[:2000], datetime.now(timezone.utc).isoformat()),
        )


async def github_request(path: str):
    if not GH_TOKEN:
        raise RuntimeError("GH_TOKEN is not configured")
    headers = {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    async with httpx.AsyncClient(timeout=20) as http:
        response = await http.get(f"https://api.github.com{path}", headers=headers)
        response.raise_for_status()
        return response.json()


async def render_request(method: str, path: str, payload: Optional[dict] = None):
    if not RENDER_API_KEY:
        raise RuntimeError("RENDER_API_KEY is not configured")
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=25) as http:
        response = await http.request(
            method, f"https://api.render.com/v1{path}", headers=headers, json=payload
        )
        response.raise_for_status()
        return response.json() if response.content else {}


@owner_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "مساعد إدارة المشاريع جاهز.\n\n"
        "/help — الأوامر\n"
        "/status — حالة خدمة Render\n"
        "/deploy — طلب نشر جديد\n"
        "/repo — معلومات مستودع GitHub\n"
        "/remember نص — حفظ معلومة للمشروع\n"
        "/memory — عرض الذاكرة\n"
        "/clearhistory — مسح سجل المحادثة المحفوظ\n\n"
        "اكتب رسالتك للتحدث مع نموذج الذكاء الاصطناعي المضبوط في الإعدادات. "
        "هذا البوت لا يشارك جلسة ChatGPT الحالية حرفيًا."
    )


@owner_only
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


@owner_only
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not RENDER_SERVICE_ID:
        await update.effective_message.reply_text("أضف RENDER_SERVICE_ID في متغيرات البيئة.")
        return
    try:
        service = await render_request("GET", f"/services/{RENDER_SERVICE_ID}")
        deploys = await render_request("GET", f"/services/{RENDER_SERVICE_ID}/deploys?limit=1")
        latest = deploys[0].get("deploy", deploys[0]) if deploys else {}
        await update.effective_message.reply_text(
            f"الخدمة: {service.get('name', RENDER_SERVICE_ID)}\n"
            f"الحالة: {service.get('suspended', 'غير معروف')}\n"
            f"آخر نشر: {latest.get('status', 'لا توجد بيانات')}\n"
            f"Commit: {str(latest.get('commit', {}).get('id', 'غير متاح'))[:12]}"
        )
    except Exception as exc:
        log.exception("Render status failed")
        await update.effective_message.reply_text(f"تعذر جلب الحالة: {str(exc)[:500]}")


@owner_only
async def deploy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not RENDER_SERVICE_ID:
        await update.effective_message.reply_text("أضف RENDER_SERVICE_ID أولًا.")
        return
    if not context.args or context.args[0].lower() != "confirm":
        await update.effective_message.reply_text(
            "هذا الأمر سيطلب نشرًا جديدًا. للتأكيد أرسل: /deploy confirm"
        )
        return
    try:
        result = await render_request("POST", f"/services/{RENDER_SERVICE_ID}/deploys", {})
        deploy = result.get("deploy", result)
        await update.effective_message.reply_text(
            f"تم إرسال طلب النشر إلى Render.\nالمعرّف: {deploy.get('id', 'غير متاح')}\n"
            f"الحالة: {deploy.get('status', 'قيد المعالجة')}"
        )
    except Exception as exc:
        log.exception("Render deploy failed")
        await update.effective_message.reply_text(f"فشل طلب النشر: {str(exc)[:500]}")


@owner_only
async def repo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GH_REPO or "/" not in GH_REPO:
        await update.effective_message.reply_text("أضف GH_REPO بصيغة owner/repository.")
        return
    try:
        repo = await github_request(f"/repos/{GH_REPO}")
        await update.effective_message.reply_text(
            f"المستودع: {repo['full_name']}\n"
            f"الفرع الأساسي: {repo['default_branch']}\n"
            f"الرؤية: {repo['visibility']}\n"
            f"آخر تحديث: {repo.get('updated_at', 'غير متاح')}\n"
            f"{repo['html_url']}"
        )
    except Exception as exc:
        log.exception("GitHub repo lookup failed")
        await update.effective_message.reply_text(f"تعذر الوصول للمستودع: {str(exc)[:500]}")


@owner_only
async def remember_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = " ".join(context.args).strip()
    if not note:
        await update.effective_message.reply_text("استعمل: /remember وصف المعلومة أو آخر خطوة")
        return
    add_memory(OWNER_ID, note)
    await update.effective_message.reply_text("حُفظت المعلومة في ذاكرة البوت.")


@owner_only
async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    notes = get_memories(OWNER_ID)
    if not notes:
        await update.effective_message.reply_text("الذاكرة فارغة. استعمل /remember لحفظ أول معلومة.")
        return
    await update.effective_message.reply_text("\n\n".join(f"• {n}" for n in notes)[-3800:])


@owner_only
async def clearhistory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with db() as con:
        con.execute("DELETE FROM messages WHERE user_id=?", (OWNER_ID,))
    await update.effective_message.reply_text("تم مسح سجل المحادثة المحفوظ. ذاكرة المشاريع لم تُمسح.")


async def chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    message = update.effective_message
    if not message or not message.text:
        return
    if not client:
        await message.reply_text("أضف AI_API_KEY وAI_BASE_URL وAI_MODEL في متغيرات البيئة.")
        return
    user_id = update.effective_user.id
    user_text = message.text.strip()
    memories = get_memories(user_id)
    history = get_history(user_id)
    system_prompt = (
        "أنت مساعد تقني يدير مشاريع المستخدم عبر محادثة Telegram. أجب بلغة المستخدم وبخطوات عملية. "
        "لا تدّع تنفيذ أي عملية إلا إذا أكّدتها أداة فعلًا. لا تدّع أنك تشارك جلسة ChatGPT الحالية. "
        "لا تطلب أسرارًا أو مفاتيح API في المحادثة؛ اطلب وضعها في متغيرات البيئة. "
        "أنت لا تملك أدوات تنفيذ كود أو تعديل ملفات GitHub من خلال المحادثة وحدها؛ "
        "الأوامر المتاحة حاليًا هي حالة Render، طلب النشر بعد التأكيد، معلومات GitHub، والذاكرة. "
        "اطلب التأكيد قبل أي إجراء يسبب تغييرًا خارجيًا.\n\n"
        "ذاكرة المشاريع المحفوظة:\n" + ("\n".join(f"- {m}" for m in memories) if memories else "(فارغة)")
    )
    save_message(user_id, "user", user_text)
    try:
        response = await client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "system", "content": system_prompt}, *history,
                      {"role": "user", "content": user_text}],
            temperature=0.3,
            max_tokens=1200,
        )
        answer = response.choices[0].message.content or "لم يصل رد نصي من النموذج."
        save_message(user_id, "assistant", answer)
        # Telegram message limit is 4096 characters.
        for i in range(0, len(answer), 3900):
            await message.reply_text(answer[i:i + 3900])
    except Exception as exc:
        log.exception("AI request failed")
        await message.reply_text(f"تعذر الاتصال بالذكاء الاصطناعي: {str(exc)[:500]}")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("monitor", monitor_cmd))
    app.add_handler(CommandHandler("deploy", deploy_cmd))
    app.add_handler(CommandHandler("repo", repo_cmd))
    app.add_handler(CommandHandler("remember", remember_cmd))
    app.add_handler(CommandHandler("memory", memory_cmd))
    app.add_handler(CommandHandler("clearhistory", clearhistory_cmd))
    async def post_init(application):
        global MONITOR_TASK
        ids = MONITOR_RENDER_SERVICE_IDS or ([RENDER_SERVICE_ID] if RENDER_SERVICE_ID else [])
        if RENDER_API_KEY and ids:
            MONITOR_TASK = asyncio.create_task(
                monitor_loop(application.bot, ids, RENDER_API_KEY, OWNER_ID,
                             interval_seconds=MONITOR_INTERVAL_SECONDS, ai_client=client,
                             ai_model=AI_MODEL, ai_base_url=AI_BASE_URL)
            )
            log.info("Render monitor started for %d service(s)", len(ids))
        else:
            log.info("Render monitor disabled: configure RENDER_API_KEY and MONITOR_RENDER_SERVICE_IDS")

    async def post_shutdown(application):
        global MONITOR_TASK
        if MONITOR_TASK:
            MONITOR_TASK.cancel()
            try:
                await MONITOR_TASK
            except asyncio.CancelledError:
                pass

    app.post_init = post_init
    app.post_shutdown = post_shutdown
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat))
    log.info("Telegram Project Assistant starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
