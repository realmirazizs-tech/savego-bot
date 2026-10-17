import hmac
import time
import asyncio
import logging
import subprocess
import sys
import tempfile
import os
import shutil
import json
import re
from dataclasses import dataclass
from urllib.parse import parse_qs
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from dotenv import dotenv_values
from PIL import Image, ImageOps
from telegram import InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

BASE = Path(__file__).resolve().parent
CONFIG = dotenv_values(BASE / ".env")
TOKEN = os.getenv("BOT_TOKEN") or CONFIG.get("BOT_TOKEN", "")
LIMIT = 45 * 1024 * 1024

ACCESS_CODE = os.getenv("ACCESS_CODE") or CONFIG.get("ACCESS_CODE", "")

authorized_users = set()
login_attempts = {}

DOMAINS = (
    "instagram.com",
    "tiktok.com",
    "facebook.com",
    "fb.watch",
    "youtube.com",
    "youtu.be",
)

logging.basicConfig(level=logging.ERROR)


# ---------- LINKNI TEKSHIRISH ----------

def valid_link(text):
    try:
        url = urlparse(text)
        host = (url.hostname or "").lower()
        return (
            url.scheme == "https"
            and not url.username
            and not url.password
            and any(
                host == domain or host.endswith("." + domain)
                for domain in DOMAINS
            )
        )
    except ValueError:
        return False


# Optional local settings in .env:
# COOKIES_BROWSER=firefox (or chrome / edge); only used when explicitly set.
# COOKIES_FILE=C:/private/instagram-cookies.txt (Netscape format)
# Never send cookies, tokens or passwords to other people.
COOKIES_BROWSER = os.getenv("COOKIES_BROWSER") or CONFIG.get("COOKIES_BROWSER", "")
COOKIES_FILE = os.getenv("COOKIES_FILE") or CONFIG.get("COOKIES_FILE", "")
JOB_TIMEOUT = 900


@dataclass
class DownloadResult:
    files: list
    diagnostic: str = ""
    incomplete: bool = False


class MediaFailure(Exception):
    pass


def safe_error(text):
    text = str(text)
    for secret in (TOKEN, ACCESS_CODE, COOKIES_FILE):
        if secret:
            text = text.replace(secret, "[hidden]")
    text = re.sub(r"https?://\S+", "[link]", text)
    text = re.sub(r"(?i)(sessionid|csrftoken|authorization|cookie)\s*[:=]\s*\S+", r"\1=[hidden]", text)
    return text[-1800:]


def explain_error(text):
    low = str(text).lower()
    if any(x in low for x in ("login", "log in", "sign in", "cookies", "authentication", "private", "accounts/login")):
        return "Sayt akkauntga kirishni talab qilyapti yoki sessiya yaroqsiz. Ochiq postda ham shunday bo'lishi mumkin."
    if any(x in low for x in ("429", "rate limit", "too many requests")):
        return "Sayt juda ko'p so'rov sabab vaqtincha chekladi. Birozdan keyin urinib ko'ring."
    if any(x in low for x in ("javascript", "js runtime", "n challenge", "n-challenge", "signature", "ejs")):
        return "YouTube uchun JavaScript runtime yoki yt-dlp yangilanishi kerak. PowerShell xatosini tekshiring."
    if "format" in low and any(x in low for x in ("not available", "requested", "no video")):
        return "Sayt yuklashga yaroqli video formatini bermadi. yt-dlp ni yangilash yoki sessiya talab qilinishi mumkin."
    if any(x in low for x in ("403", "forbidden", "blocked", "captcha")):
        return "Sayt bu ulanishni blokladi yoki tekshiruv talab qilyapti."
    if "timeout" in low or "timed out" in low:
        return "Yuklash yoki o'girish uchun vaqt tugadi. Internet tezligini tekshiring."
    if "space" in low or "disk" in low:
        return "Vaqtinchalik fayllar uchun diskda joy yetarli emas."
    if "live" in low or "jonli" in low:
        return "Jonli efirlar qo'llab-quvvatlanmaydi."
    if "compress" in low or "kichray" in low:
        return "Videoni 45 MB gacha kichraytirib bo'lmadi. Qisqaroq video yoki Local Bot API kerak."
    if "unsupported" in low:
        return "Bu link turi yuklovchida qo'llab-quvvatlanmayapti. Postning to'g'ridan-to'g'ri linkini yuboring."
    return "Saytdan media olinmadi. PowerShell oynasida maxfiy ma'lumotlari yashirilgan xato ko'rsatiladi."


def platform(url):
    host = (urlparse(url).hostname or "").lower()
    if host == "youtu.be" or host.endswith("youtube.com"):
        return "youtube"
    if host.endswith("instagram.com"):
        return "instagram"
    if host.endswith("tiktok.com"):
        return "tiktok"
    return "facebook"


def is_content_url(url):
    parsed = urlparse(url)
    path = parsed.path.lower()
    site = platform(url)
    if site == "youtube":
        return (parsed.hostname == "youtu.be" and bool(path.strip("/"))) or any(x in path for x in ("/watch", "/shorts/", "/live/", "/embed/"))
    if site == "instagram":
        return any(x in path for x in ("/p/", "/reel/", "/reels/", "/tv/", "/share/"))
    if site == "tiktok":
        return any(x in path for x in ("/video/", "/photo/", "/t/")) or parsed.hostname in ("vm.tiktok.com", "vt.tiktok.com")
    return parsed.hostname == "fb.watch" or any(x in path for x in ("/photo", "/video", "/watch", "/posts/", "/share/", "/reel/", "/story.php", "/permalink.php", "/media/set"))


def run_process(args, timeout=JOB_TIMEOUT):
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        raise MediaFailure("timeout: jarayon uchun vaqt tugadi") from None


def cookie_args():
    if COOKIES_FILE:
        return ["--cookies", str(Path(COOKIES_FILE).expanduser())]
    if COOKIES_BROWSER:
        return ["--cookies-from-browser", COOKIES_BROWSER]
    return []


def list_media(folder):
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif", ".mp4", ".mkv", ".webm", ".mov", ".m4v"}
    # Filenames start with the extractor's item number; retain album order.
    return sorted((f for f in Path(folder).rglob("*") if f.is_file() and f.suffix.lower() in allowed),
                  key=lambda f: (f.name, str(f)))


def download_gallery(url, folder):
    folder.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "gallery_dl", "--config-ignore", "--no-input",
               "--directory", str(folder), "--filename", "{num:06}_{filename}.{extension}",
               "--retries", "1", "--http-timeout", "30"]
    command += cookie_args() + [url]
    try:
        result = run_process(command)
        diagnostic = safe_error(result.stderr)
        incomplete = result.returncode != 0
    except MediaFailure as error:
        diagnostic, incomplete = str(error), True
    return DownloadResult(list_media(folder), diagnostic, incomplete)


def prepare_photo(source, target):
    with Image.open(source) as opened:
        picture = ImageOps.exif_transpose(opened).convert("RGB")
        picture.thumbnail((2560, 2560))
        w, h = picture.size
        if max(w, h) > 19 * min(w, h):
            side = (max(w, h) + 18) // 19
            canvas = Image.new("RGB", (max(w, side), max(h, side)), "white")
            canvas.paste(picture, ((canvas.width-w)//2, (canvas.height-h)//2))
            picture = canvas
        picture.save(target, "JPEG", quality=90)
    return target


async def send_photos(message, files):
    # Telegram albomlari 10 tadan yuboriladi.
    for offset in range(0, len(files), 10):
        batch = files[offset:offset + 10]

        with ExitStack() as stack:
            handles = [
                stack.enter_context(path.open("rb"))
                for path in batch
            ]

            if len(handles) == 1:
                await message.reply_photo(
                    photo=handles[0],
                    read_timeout=120,
                    write_timeout=120,
                )
            else:
                await message.reply_media_group(
                    media=[
                        InputMediaPhoto(media=file)
                        for file in handles
                    ],
                    read_timeout=120,
                    write_timeout=120,
                )

        if offset + 10 < len(files):
            await asyncio.sleep(3)


# ---------- VIDEO VA MP3 ----------

def download_video(url, folder):
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "completed.txt"
    # Only finished, merged outputs are sent; never unfinished stream fragments.
    command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
               "--no-progress", "--no-mtime", "--socket-timeout", "30", "--retries", "2",
               "--match-filter", "!is_live", "--merge-output-format", "mp4",
               "-f", "bv[height<=720]+ba/b[height<=720]/bv+ba/b",
               "-o", str(folder / "%(autonumber)06d_%(id)s.%(ext)s"),
               "--print-to-file", "after_move:filepath", str(manifest)]
    # Newer YouTube extraction can use a locally installed Deno/Node runtime.
    if shutil.which("deno"):
        command += ["--js-runtimes", "deno"]
    elif shutil.which("node"):
        command += ["--js-runtimes", "node"]
    command += cookie_args() + [url]
    try:
        result = run_process(command)
        diagnostic = safe_error(result.stderr)
        incomplete = result.returncode != 0
    except MediaFailure as error:
        diagnostic, incomplete = str(error), True
    completed = []
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            path = Path(line)
            if path.is_file() and path.resolve().is_relative_to(folder.resolve()):
                if path not in completed:
                    completed.append(path)
    return DownloadResult(completed, diagnostic, incomplete)


def collect_media(url, folder):
    if platform(url) == "youtube":
        result = download_video(url, folder / "video")
    else:
        # Inspect every social post with the gallery extractor, including short
        # share URLs. Download BOTH images and videos from mixed carousels.
        result = download_gallery(url, folder / "gallery")
        if not result.files:
            fallback = download_video(url, folder / "video")
            if fallback.files:
                result = fallback
            else:
                result.diagnostic += "\n" + fallback.diagnostic
    if not result.files:
        raise MediaFailure(result.diagnostic or "No media found / unsupported post")
    return result


def probe(path):
    result = run_process(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", str(path)], timeout=60)
    if result.returncode:
        raise MediaFailure(safe_error(result.stderr))
    info = json.loads(result.stdout)
    duration = float(info.get("format", {}).get("duration") or 0)
    streams = info.get("streams", [])
    has_audio = any(x.get("codec_type") == "audio" for x in streams)
    video = next((x for x in streams if x.get("codec_type") == "video"), {})
    return duration, has_audio, video.get("codec_name", "")


def prepare_video(source, target):
    duration, has_audio, codec = probe(source)
    if source.stat().st_size <= LIMIT and source.suffix.lower() == ".mp4" and codec == "h264":
        return source
    if duration <= 0:
        raise MediaFailure("compress: video davomiyligi aniqlanmadi")
    # Budget below the API limit; never truncate the input video.
    audio_rate = 64000 if has_audio else 0
    budget = int((min(40 * 1024 * 1024, LIMIT * 0.88) * 8 / duration) * 0.94)
    video_rate = budget - audio_rate
    if video_rate < 45000:
        raise MediaFailure("compress: video juda uzun")
    for attempt in range(2):
        rate = min(2_000_000, int(video_rate * (0.75 ** attempt)))
        height = 720 if rate >= 650000 else (480 if rate >= 200000 else 240)
        vf = f"scale=-2:'min({height},ih)',pad=ceil(iw/2)*2:ceil(ih/2)*2"
        result = run_process([
            "ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(source),
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-b:v", str(rate), "-maxrate", str(rate), "-bufsize", str(rate*2),
            "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(target)
        ], timeout=max(600, int(duration * 4)))
        if result.returncode:
            raise MediaFailure(safe_error(result.stderr))
        if target.stat().st_size <= LIMIT:
            return target
    raise MediaFailure("compress: kichraytirilgan video ham 45 MB dan katta")


def extract_audio(source, target):
    duration, has_audio, _ = probe(source)
    if not has_audio:
        return None
    if duration <= 0:
        raise MediaFailure("MP3 davomiyligi aniqlanmadi")
    budget = int(min(40 * 1024 * 1024, LIMIT * 0.88) * 8 / duration / 1000)
    rates = [r for r in (128, 96, 64, 48, 32, 24, 16, 8) if r <= budget]
    if not rates:
        raise MediaFailure("MP3 45 MB dan katta: audio juda uzun")
    result = run_process([
        "ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(source),
        "-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-b:a", f"{rates[0]}k", str(target)
    ], timeout=max(600, int(duration * 2)))
    if result.returncode:
        raise MediaFailure(safe_error(result.stderr))
    if target.stat().st_size > LIMIT:
        raise MediaFailure("MP3 45 MB dan katta")
    return target


# ---------- BOT XABARLARI ----------

WELCOME = (
    "👋 Video yoki rasm linkini yuboring.\n"
    "📥 Instagram • TikTok • Facebook • YouTube\n"
    "🎵 Video ovozi MP3 qilib ham yuboriladi."
)


async def start(update, context):
    message = update.message
    user = update.effective_user
    if not message or not user:
        return
    if message.chat.type != "private":
        await message.reply_text("Botning shaxsiy chatiga yozing.")
        return
    await message.reply_text(
        WELCOME if user.id in authorized_users
        else "🔐 Maxfiy kodni kiriting."
    )


async def logout(update, context):
    message = update.message
    user = update.effective_user
    if not message or not user or message.chat.type != "private":
        return
    authorized_users.discard(user.id)
    await message.reply_text("🔒 Chiqdingiz. Qayta kirish uchun maxfiy kodni yuboring.")


async def check_access(update, context):
    message = update.message
    user = update.effective_user
    if not message or not message.text or not user:
        return False
    if message.chat.type != "private":
        await message.reply_text("Botning shaxsiy chatiga yozing.")
        return False
    if user.id in authorized_users:
        return True

    entered_code = message.text
    try:
        await message.delete()
    except Exception:
        pass

    async def respond(text):
        await context.bot.send_message(chat_id=message.chat_id, text=text)

    now = time.monotonic()
    attempts, blocked_until = login_attempts.get(user.id, (0, 0))
    if now < blocked_until:
        seconds = int(blocked_until - now) + 1
        await respond(f"⏳ {seconds} soniyadan keyin qayta urinib ko‘ring.")
        return False
    if blocked_until:
        attempts = 0

    if ACCESS_CODE and hmac.compare_digest(
        entered_code.encode("utf-8"), ACCESS_CODE.encode("utf-8")
    ):
        authorized_users.add(user.id)
        login_attempts.pop(user.id, None)
        await respond("✅ Kod to‘g‘ri!\n\n" + WELCOME)
    else:
        attempts += 1
        if attempts >= 5:
            login_attempts[user.id] = (0, now + 300)
            await respond("🔒 5 marta xato kod. 5 daqiqadan keyin urinib ko‘ring.")
        else:
            login_attempts[user.id] = (attempts, 0)
            await respond("❌ Maxfiy kod noto‘g‘ri. Qayta kiriting.")
    # Kod yozilgan xabar link sifatida qayta ishlanmaydi.
    return False


async def handle_media(update, context):
    if not await check_access(update, context):
        return
    message = update.message
    url = message.text.strip()
    if not valid_link(url) or not is_content_url(url):
        await message.reply_text("Profil yoki kanal emas, post/video linkini https:// bilan yuboring.")
        return
    status = await message.reply_text("Media yuklanmoqda. Katta post biroz vaqt olishi mumkin...")
    try:
        with tempfile.TemporaryDirectory(prefix="savego_") as temporary:
            folder = Path(temporary)
            result = await asyncio.to_thread(collect_media, url, folder)
            if result.diagnostic:
                print("Yuklovchi:", safe_error(result.diagnostic), flush=True)
            prepared = folder / "prepared"
            prepared.mkdir()
            failures = []
            count = 0
            photo_batch = []

            async def flush_photos():
                nonlocal count
                if not photo_batch:
                    return
                try:
                    await send_photos(message, photo_batch)
                    count += len(photo_batch)
                except Exception as error:
                    failures.append("Rasm yuborish: " + explain_error(str(error)))
                    print("Telegram rasm:", safe_error(error), flush=True)
                photo_batch.clear()

            for index, source in enumerate(result.files):
                if source.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}:
                    try:
                        photo = await asyncio.to_thread(prepare_photo, source, prepared / f"{index}.jpg")
                        photo_batch.append(photo)
                        if len(photo_batch) == 10:
                            await flush_photos()
                            await asyncio.sleep(3)
                    except Exception as error:
                        failures.append("Bitta rasmni tayyorlab bo'lmadi.")
                        print("Rasm:", safe_error(error), flush=True)
                    continue
                await flush_photos()
                await status.edit_text(f"{index+1}/{len(result.files)}: video tayyorlanmoqda; katta bo'lsa kichraytiriladi...")
                try:
                    video = await asyncio.to_thread(prepare_video, source, prepared / f"{index}.mp4")
                    with video.open("rb") as stream:
                        await message.reply_video(video=stream, supports_streaming=True,
                                                  read_timeout=180, write_timeout=180)
                    count += 1
                except Exception as error:
                    failures.append("Video: " + explain_error(str(error)))
                    print("Video:", safe_error(error), flush=True)
                # Even if video compression fails, still try delivering the audio.
                try:
                    audio = await asyncio.to_thread(extract_audio, source, prepared / f"{index}.mp3")
                    if audio:
                        with audio.open("rb") as stream:
                            await message.reply_audio(audio=stream, title="Video ovozi",
                                                      read_timeout=180, write_timeout=180)
                    else:
                        await message.reply_text("Bu videoda ovoz yo'q; MP3 yaratilmadi.")
                except Exception as error:
                    failures.append("MP3: " + explain_error(str(error)))
                    print("MP3:", safe_error(error), flush=True)
                await asyncio.sleep(1)
            await flush_photos()
            summary = f"Tayyor: {count} ta rasm/video yuborildi."
            if result.incomplete:
                summary += "\nPost to'liq olinmagan bo'lishi mumkin. " + explain_error(result.diagnostic)
            if failures:
                summary += "\n" + "\n".join(dict.fromkeys(failures))
            await status.edit_text(summary[:3900])
    except Exception as error:
        print("Yuklash xatosi:", safe_error(error), flush=True)
        await status.edit_text(explain_error(str(error)))


async def on_error(update, context):
    print("Bot xatosi:", safe_error(context.error), flush=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(".env faylida BOT_TOKEN topilmadi.")

    if not ACCESS_CODE or ACCESS_CODE.startswith("BU_YERGA"):
        raise SystemExit(".env fayliga ACCESS_CODE=oz_maxfiy_kodingiz yozing.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("FFmpeg yoki FFprobe topilmadi. PowerShell da ffmpeg -version ni tekshiring.")

    if COOKIES_FILE and not Path(COOKIES_FILE).expanduser().is_file():
        raise SystemExit("COOKIES_FILE ko'rsatgan fayl topilmadi.")
    if not shutil.which("deno") and not shutil.which("node"):
        print("Eslatma: YouTube ayrim linklari uchun Deno yoki Node.js talab qilinishi mumkin.")
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_media,
        )
    )
    app.add_error_handler(on_error)

    print("Bot ishga tushmoqda. To'xtatish: Ctrl+C")
    app.run_polling(drop_pending_updates=True)