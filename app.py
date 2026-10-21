"""UIS v60 outbound relay. Streams chunks to Telegram; no media files on disk.
Keeps the existing Cloudflare webhook. Never calls getUpdates or setWebhook.
"""
import asyncio
import hmac
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from telethon import TelegramClient, types

MAX_SIZE = 2000 * 1024 * 1024
PART_SIZE = 512 * 1024
OWNER = os.environ.get('TELEGRAM_OWNER_ID', '')
SECRET = os.environ.get('TELEGRAM_RELAY_SECRET', '')
ORIGIN = os.environ.get('UIS_ORIGIN', '').rstrip('/')
TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
API_ID = os.environ.get('TELEGRAM_API_ID', '')
API_HASH = os.environ.get('TELEGRAM_API_HASH', '')
DATA = Path(os.environ.get('DATA_DIR', './uis-relay-data'))
logging.basicConfig(level=logging.WARNING)
logging.getLogger('telethon').setLevel(logging.CRITICAL)


def validate_job(value):
    if not isinstance(value, dict):
        raise ValueError('invalid job')
    ident = str(uuid.UUID(value['id']))
    source = urlsplit(value['url'])
    base = urlsplit(ORIGIN)
    if (source.scheme != 'https' or not base.netloc or
            source.netloc != base.netloc or source.path != '/telegram/file' or
            source.username or source.password or source.fragment or
            not source.query.startswith('token=')):
        raise ValueError('invalid source')
    size = value.get('size')
    if type(size) is not int or not 0 < size <= MAX_SIZE:
        raise ValueError('invalid size')
    if str(value.get('chat_id')) != OWNER or not OWNER.isdigit():
        raise ValueError('invalid owner')
    name = str(value.get('name') or 'file.bin').replace('\\', '/').split('/')[-1]
    name = ''.join(c for c in name if ord(c) >= 32)[:180] or 'file.bin'
    return {**value, 'id': ident, 'name': name, 'size': size}


class BoundedStream:
    """A single exact Telegram part per read. Full media never enters RAM."""
    def __init__(self, content, size, name):
        self.content, self.remaining, self.name = content, size, name

    async def read(self, size):
        if not 0 < size <= PART_SIZE:
            raise ValueError('unbounded read refused')
        amount = min(size, self.remaining)
        if not amount:
            return b''
        data = await self.content.readexactly(amount)
        self.remaining -= len(data)
        return data


class Relay:
    def __init__(self):
        self.ready = False
        self.client = None
        self.http = None
        self.queue = asyncio.Queue(maxsize=20)
        self.task = None
        self.db = None

    async def start(self, app):
        DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(DATA, 0o700)
        self.db = sqlite3.connect(DATA / 'jobs.sqlite')
        os.chmod(DATA / 'jobs.sqlite', 0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, status TEXT, created INTEGER)')
        # Never resend an uncertain in-flight delivery after a restart.
        self.db.execute("UPDATE jobs SET status='interrupted' WHERE status IN ('queued','sending')")
        self.db.commit()
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=20, sock_read=90), auto_decompress=False, trust_env=False)
        self.task = asyncio.create_task(self.run())

    async def connect(self):
        if not (API_ID.isdigit() and API_HASH and TOKEN and OWNER.isdigit() and len(SECRET) >= 32 and ORIGIN.startswith('https://')):
            raise ValueError('missing configuration')
        if self.client:
            await self.client.disconnect()
        self.client = TelegramClient(str(DATA / 'bot'), int(API_ID), API_HASH,
                                     receive_updates=False, request_retries=2,
                                     connection_retries=2, flood_sleep_threshold=30)
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.client.sign_in(bot_token=TOKEN)
        me = await self.client.get_me()
        if not me.bot or str(me.id) != TOKEN.split(':')[0]:
            raise ValueError('wrong bot session')
        self.ready = True
        print('UIS RELAY READY: port 5000; streaming enabled; webhook unchanged', flush=True)

    async def notify(self, text):
        try:
            async with self.http.post('https://api.telegram.org/bot' + TOKEN + '/sendMessage',
                                      json={'chat_id': OWNER, 'text': text[:3900]},
                                      timeout=aiohttp.ClientTimeout(total=20)) as response:
                await response.read()
        except Exception:
            print('UIS notice could not be delivered', flush=True)

    async def transfer(self, job):
        # Tickets may be followed only on UIS_ORIGIN, without redirects.
        async with self.http.get(job['url'], allow_redirects=False) as response:
            if response.status != 200 or int(response.headers.get('Content-Length', 0)) != job['size']:
                raise ValueError('source changed or unavailable')
            stream = BoundedStream(response.content, job['size'], job['name'])
            uploaded = await self.client.upload_file(stream, file_size=job['size'],
                                                     file_name=job['name'], part_size_kb=512)
            if stream.remaining:
                raise ValueError('incomplete stream')
            # Known private owner; bot authorization accepts zero access hash.
            await self.client.send_file(types.InputPeerUser(int(OWNER), 0), uploaded,
                                        force_document=True, caption=job['name'], parse_mode=None)

    async def run(self):
        while not self.ready:
            try:
                await self.connect()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print('UIS relay not ready: ' + type(error).__name__ + '; check environment variables / Telegram connectivity', flush=True)
                await asyncio.sleep(30)
        while True:
            job = await self.queue.get()
            try:
                self.db.execute("UPDATE jobs SET status='sending' WHERE id=?", (job['id'],))
                self.db.commit()
                await asyncio.wait_for(self.transfer(job), timeout=10800)
                status = 'sent'
                print('UIS transfer sent: ' + job['id'], flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                status = 'failed'
                print('UIS transfer failed: ' + type(error).__name__, flush=True)
                await self.notify('Fayl uzatish tugamadi: ' + job['name'] + '\n/browse orqali yangidan tanlang. Avval fayl kelgan-kelmaganini tekshiring.')
            finally:
                self.queue.task_done()
            self.db.execute('UPDATE jobs SET status=? WHERE id=?', (status, job['id']))
            self.db.commit()

    async def stop(self, app):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        if self.client:
            await self.client.disconnect()
        if self.http:
            await self.http.close()
        if self.db:
            self.db.close()

    async def health(self, request):
        return web.json_response({'service': 'UIS Telegram Transfer', 'status': 'ok',
                                  'transfer_enabled': self.ready}, headers={'Cache-Control': 'no-store'})

    async def submit(self, request):
        if len(SECRET) < 32 or not hmac.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + SECRET):
            raise web.HTTPForbidden()
        if not self.ready:
            return web.json_response({'error': 'not_ready'}, status=503)
        try:
            job = validate_job(await request.json())
        except (KeyError, ValueError, TypeError, AttributeError):
            raise web.HTTPBadRequest()
        existing = self.db.execute('SELECT status FROM jobs WHERE id=?', (job['id'],)).fetchone()
        if existing:
            return web.json_response({'id': job['id'], 'status': existing[0]})
        if self.queue.full():
            return web.json_response({'error': 'queue_full'}, status=429)
        self.db.execute("DELETE FROM jobs WHERE created < ? AND status NOT IN ('queued','sending')", (int(time.time()) - 604800,))
        self.db.execute('INSERT INTO jobs VALUES (?,?,?)', (job['id'], 'queued', int(time.time())))
        self.db.commit()
        self.queue.put_nowait(job)
        return web.json_response({'id': job['id'], 'status': 'queued'}, status=202)


def make_app(relay=None):
    relay = relay or Relay()
    app = web.Application(client_max_size=16384)
    app.router.add_get('/', relay.health)
    app.router.add_get('/healthz', relay.health)
    app.router.add_post('/jobs', relay.submit)
    app.on_startup.append(relay.start)
    app.on_cleanup.append(relay.stop)
    return app


if __name__ == '__main__':
    os.umask(0o077)
    web.run_app(make_app(), host='0.0.0.0', port=int(os.environ.get('PORT', '5000')), access_log=None)
