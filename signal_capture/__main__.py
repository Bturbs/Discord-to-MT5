import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import os
import threading
import time

import discord

from .config import load_config, SOURCE_BOT_ID
from .execution import Executor
from .ledger import AzureLedger, SQLiteLedger, Busy
from .parser import extract_text
from .service import Service, Event

log = logging.getLogger("capture")


def make_ledger():
    connection = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    # Same account always uses the same partition, across revisions and modes.
    identity = f"{os.environ['MT5_SERVER']}:{int(os.environ['MT5_LOGIN'])}"
    partition = hashlib.sha256(identity.encode()).hexdigest()[:32]
    if connection:
        return AzureLedger(connection, partition)
    if os.getenv("CONTAINER_APP_NAME"):
        raise RuntimeError("Azure requires AZURE_STORAGE_CONNECTION_STRING; refusing ephemeral audit storage")
    return SQLiteLedger(os.getenv("LEDGER_PATH", "state/ledger.db"))


class Health:
    def __init__(self):
        self.pulse = time.monotonic()
        self.mt5_ok = False
        self.blocked = False
        self.client = None

    def start(self):
        health = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                alive = time.monotonic() - health.pulse < 120
                ready = alive and health.mt5_ok and not health.blocked and health.client is not None and health.client.is_ready()
                ok = alive if self.path == "/health/live" else ready
                self.send_response((200 if ok else 503) if self.path in ("/health/live", "/health/ready") else 404)
                self.end_headers()
                self.wfile.write(b"ok" if ok else b"unavailable")

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


class CaptureClient(discord.Client):
    def __init__(self, config, service, health, **kwargs):
        super().__init__(**kwargs)
        self.config, self.service, self.health = config, service, health
        self.queue = asyncio.Queue(maxsize=500)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5")

    async def setup_hook(self):
        self.worker_task = asyncio.create_task(self.worker())

    async def on_ready(self):
        log.info("Discord connected; source_bot=%s channels=%s dry_run=%s", SOURCE_BOT_ID, list(self.config.channels), self.config.dry_run)

    async def on_message(self, message):
        # Do NOT discard all bot messages: the authorized source is itself a bot.
        if message.author.id != SOURCE_BOT_ID or message.channel.id not in self.config.channels or message.webhook_id:
            return
        event = Event(str(message.id), message.author.id, message.channel.id,
                      message.created_at.timestamp(), extract_text(message))
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            log.error("message=%s dropped: queue full", message.id)

    async def call(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self.pool, fn, *args)

    async def worker(self):
        while not self.is_closed():
            self.health.pulse = time.monotonic()
            try:
                await self.call(self.service.executor.account)
                self.health.mt5_ok = True
                self.health.blocked = bool(await self.call(self.service.ledger.get, "execution-gate"))
            except Exception:
                self.health.mt5_ok = False
                log.error("MT5/storage health check failed")
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=10)
            except TimeoutError:
                continue
            try:
                while True:
                    try:
                        await self.call(self.service.process, event)
                        break
                    except Busy:
                        self.health.blocked = True
                        self.health.pulse = time.monotonic()
                        if time.time() - event.created_at > self.config.max_signal_age_seconds:
                            log.error("message=%s skipped: execution gate remained locked until signal expired", event.message_id)
                            break
                        await asyncio.sleep(1)
            except Exception as exc:
                self.health.blocked = True
                # Do not dump SDK exceptions that might contain request credentials.
                log.error("message=%s halted (%s); inspect durable ledger and MT5 history", event.message_id, type(exc).__name__)
            finally:
                self.queue.task_done()


async def main():
    import MetaTrader5 as mt5
    config = load_config()
    for key in ("DISCORD_BOT_TOKEN", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "MT5_PATH"):
        if not os.getenv(key):
            raise ValueError(f"Missing environment variable {key}")
    health = Health()
    health.start()
    ledger = make_ledger()
    login = int(os.environ["MT5_LOGIN"])
    # Initialize the terminal before subscribing to new Discord messages.
    connected = await asyncio.to_thread(mt5.initialize, os.environ["MT5_PATH"], login=login,
                                        password=os.environ["MT5_PASSWORD"], server=os.environ["MT5_SERVER"],
                                        timeout=60000, portable=os.getenv("MT5_PORTABLE", "true").lower() == "true")
    if not connected:
        raise RuntimeError("MT5 initialization failed; verify terminal setup, login and broker server")
    executor = Executor(mt5, config, login, os.environ["MT5_SERVER"])
    intents = discord.Intents.none()
    intents.guilds = intents.guild_messages = intents.message_content = True
    client = CaptureClient(config, Service(config, executor, ledger), health, intents=intents)
    health.client = client
    try:
        async with client:
            await client.start(os.environ["DISCORD_BOT_TOKEN"])
    finally:
        task = getattr(client, "worker_task", None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Let an in-flight broker call finish before closing IPC.
        client.pool.shutdown(wait=True)
        mt5.shutdown()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
