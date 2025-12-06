import asyncio
import requests
from collections import defaultdict

from x1.bot.utils import Utils
from x1.bot.utils.LoggerWrapper import LoggerWrapper


class TelegramMessageQueue:
    def __init__(self, bot_token, log: LoggerWrapper):
        self.tag = "TelegramMessage"
        self.log = log
        self.bot_token = bot_token
        self.queue = asyncio.Queue()
        self.event_task = None
        self.flush_task = None
        self.limit_reset_task = None

        self.message_buffer = defaultdict(list)      # Gom tin theo group
        self.buffer_lock = asyncio.Lock()            # Đồng bộ truy cập buffer

    async def start(self):
        self.event_task = asyncio.create_task(self._worker())
        self.flush_task = asyncio.create_task(self._flush_messages())
        self.log.d(self.tag, "start done")

    async def stop(self):
        await Utils.stop_task(self.event_task)
        await Utils.stop_task(self.flush_task)
        self.log.d(self.tag, "stop done")

    async def send_user_message(self, text, chat_id):
        """Đưa tin nhắn vào queue"""
        await self.queue.put((text, chat_id))

    async def send_admin_message(self, text):
        """Đưa tin nhắn vào queue"""
        await self.queue.put((text, "@binance_leaderboard"))

    async def send_message(self, text, chat_id):
        """Đưa tin nhắn vào queue"""
        await self.queue.put((text, chat_id))

    def send_telegram_message(self, text, chat_id):
        """Gửi tin nhắn đến Telegram"""
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        data = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true"
        }
        try:
            response = requests.post(url, json=data, timeout=10)
            self.log.t(self.tag, f"Sent to {chat_id} | Status: {response.status_code}")
            return False
        except requests.RequestException as e:
            self.log.e(self.tag, f"Request error: {e}")
            return True
        except Exception as e:
            self.log.e(self.tag, f"Unknown error: {e}")
            return False

    async def _worker(self):
        """Worker gom tin nhắn vào buffer"""
        self.log.i(self.tag, "start _worker")
        try:
            while True:
                text, chat_id = await self.queue.get()
                async with self.buffer_lock:
                    self.message_buffer[chat_id].append(text)
                self.queue.task_done()
        except asyncio.CancelledError:
            self.log.i(self.tag, "stop _worker")

    async def _flush_messages(self):
        """Gửi tin nhắn gộp cho từng group, mỗi 3s, tạo task riêng cho mỗi group"""
        try:
            while True:
                try:
                    await asyncio.sleep(3)
                    async with self.buffer_lock:
                        chat_ids = list(self.message_buffer.keys())

                    for chat_id in chat_ids:
                        if len(self.message_buffer[chat_id][:]) > 0:
                            asyncio.create_task(self._send_group_message(chat_id))
                            self.log.d(self.tag, f"_flush_messages: send {chat_id} to telegram")
                    await asyncio.sleep(0)
                except Exception as e:
                    self.log.e(self.tag, f"_flush_messages error: {e}")
        except asyncio.CancelledError:
            self.log.i(self.tag, "stop _flush_messages")

    async def _send_group_message(self, chat_id):
        """Task riêng gửi tin cho 1 group, không xóa toàn bộ buffer"""
        try:
            async with self.buffer_lock:
                if not self.message_buffer[chat_id]:
                    return

                # Sao chép tin cần gửi
                messages_to_send = self.message_buffer[chat_id][:]
                num_sent = len(messages_to_send)

            combined_message = "\n\n".join(messages_to_send)

            retry = True
            while retry:
                retry = self.send_telegram_message(combined_message, chat_id)
                await asyncio.sleep(2)
            self.log.d(self.tag, f"_send_group_message num_sent={num_sent} chat_id={chat_id}")
            # Sau khi gửi thành công, loại bỏ đúng số tin đã gửi
            async with self.buffer_lock:
                self.message_buffer[chat_id] = self.message_buffer[chat_id][num_sent:]

        except Exception as e:
            self.log.e(self.tag, f"_send_group_message error for {chat_id}: {e}")

