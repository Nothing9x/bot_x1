import asyncio
import time
from collections import deque, defaultdict
from datetime import datetime
import numpy as np
import traceback

from x1.bot.notification.notification_manager import TelegramMessageQueue
from x1.bot.utils.LoggerWrapper import LoggerWrapper


class PumpDetector:
    """
    Class phát hiện pump coin realtime từ MEXC WebSocket
    - Tính toán realtime theo từng tick (không đợi nến đóng)
    - Chỉ bắt nến pump ĐẦU TIÊN (không bắt lại khi đã pump)
    """

    def __init__(self, log: LoggerWrapper, tele_message: TelegramMessageQueue, chat_id):
        self.tag = "PumpDetector"
        self.log = log
        self.tele_message = tele_message
        self.chat_id = chat_id

        # Lưu trữ dữ liệu nến cho mỗi symbol
        # Format: {symbol: {interval: deque([candles])}}
        self.candle_history = defaultdict(lambda: defaultdict(lambda: deque(maxlen=100)))

        # Lưu các symbol đã pump gần đây (tránh detect lại)
        self.recent_pumps = {}  # {symbol: {'timestamp': ts, 'candle_timestamp': ct}}
        self.pump_lookback_candles = 20  # Kiểm tra 20 nến gần đây
        self.pump_cooldown_seconds = 600  # 10 phút cooldown

        # Config detection
        self.config = {
            # Điều kiện pump
            'price_increase_1m': 3.0,  # Tăng ít nhất 3% trong 1 phút
            'price_increase_5m': 8.0,  # Tăng ít nhất 8% trong 5 phút
            'volume_spike_multiplier': 3.0,  # Volume tăng ít nhất 3 lần
            'min_volume_usdt': 50000,  # Volume tối thiểu 50k USDT

            # RSI
            'rsi_period': 14,
            'rsi_overbought': 70,

            # Momentum
            'momentum_threshold': 2.0,  # Momentum tăng mạnh

            # Confidence score
            'min_confidence': 70,  # Confidence tối thiểu để vào lệnh

            # Pump history detection
            'recent_pump_price_threshold': 5.0,  # Nếu đã tăng >5% trong 20 nến gần đây
            'recent_pump_volume_threshold': 3.0,  # Nếu đã có volume spike >3x
        }

        # Callback khi phát hiện pump
        self.on_pump_detected = None

    def set_on_pump_detected(self, callback):
        """Đăng ký callback khi phát hiện pump"""
        self.on_pump_detected = callback

    async def on_candle_update(self, symbol: str, interval: str, candle_data: dict):
        """
        Callback nhận dữ liệu nến từ WebSocket
        - Tính toán realtime theo từng tick
        - Không đợi nến đóng
        """
        try:
            # Parse candle data
            timestamp = candle_data.get('t', 0)
            candle = {
                'timestamp': timestamp,
                'open': float(candle_data.get('o', 0)),
                'high': float(candle_data.get('h', 0)),
                'low': float(candle_data.get('l', 0)),
                'close': float(candle_data.get('c', 0)),
                'volume': float(candle_data.get('a', 0)),
            }

            # Kiểm tra xem đã có candle với timestamp này chưa
            history = self.candle_history[symbol][interval]

            if len(history) == 0:
                # Chưa có candle nào, append luôn
                history.append(candle)
                #self.log.d(self.tag, f"📊 New candle added for {symbol} {interval} at t={timestamp}")
            else:
                # Lấy candle cuối cùng
                last_candle = history[-1]

                if timestamp > last_candle['timestamp']:
                    # Timestamp mới → nến mới, append
                    history.append(candle)
                    #self.log.d(self.tag, f"📊 New candle added for {symbol} {interval} at t={timestamp}")

                    # Phân tích pump khi có nến MỚI
                    if interval == "Min1":
                        await self.analyze_pump_realtime(symbol, is_new_candle=True)

                elif timestamp == last_candle['timestamp']:
                    # Cùng timestamp → update nến hiện tại (tick update)
                    history[-1] = candle

                    # Phân tích pump REALTIME khi tick update
                    if interval == "Min1":
                        await self.analyze_pump_realtime(symbol, is_new_candle=False)
                else:
                    # timestamp cũ → dữ liệu cũ, bỏ qua
                    return

        except Exception as e:
            self.log.e(self.tag, f"Error processing candle for {symbol}: {e}\n{traceback.format_exc()}")

    async def analyze_pump_realtime(self, symbol: str, is_new_candle: bool):
        """
        Phân tích pump REALTIME
        - Tính volume theo tỷ lệ thời gian của nến hiện tại
        - Chỉ bắt nến pump đầu tiên
        """
        try:
            # Kiểm tra cooldown
            if self.is_in_cooldown(symbol):
                return

            # Kiểm tra xem đã pump gần đây chưa
            if self.has_recent_pump(symbol):
                return

            # Lấy dữ liệu nến
            candles_1m = list(self.candle_history[symbol]["Min1"])
            candles_5m = list(self.candle_history[symbol]["Min5"])

            if len(candles_1m) < 20:
                return  # Chưa đủ dữ liệu

            current_candle = candles_1m[-1]

            # 1. Tính price change realtime
            price_change_1m = self.calculate_price_change_realtime(candles_1m, 1)
            price_change_5m = self.calculate_price_change_realtime(candles_5m, 5) if len(candles_5m) >= 5 else 0

            # 2. Tính volume spike với normalization theo thời gian
            volume_ratio = self.calculate_volume_spike_realtime(candles_1m)
            current_volume_usdt = current_candle['volume'] * current_candle['close']

            # 3. Tính RSI
            rsi = self.calculate_rsi(candles_1m)

            # 4. Tính momentum
            momentum = self.calculate_momentum(candles_1m)

            # 5. Kiểm tra buy pressure (nến xanh liên tiếp)
            buy_pressure = self.calculate_buy_pressure(candles_1m)

            # 6. Kiểm tra điều kiện pump
            is_pump = (
                    price_change_1m >= self.config['price_increase_1m'] and
                    volume_ratio >= self.config['volume_spike_multiplier'] and
                    current_volume_usdt >= self.config['min_volume_usdt']
            )

            if is_pump:
                # Tính confidence score
                confidence = self.calculate_confidence(
                    price_change_1m, price_change_5m, volume_ratio,
                    rsi, momentum, buy_pressure
                )

                # Nếu confidence đủ cao, phát tín hiệu
                if confidence >= self.config['min_confidence']:
                    pump_signal = {
                        'symbol': symbol,
                        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'candle_timestamp': current_candle['timestamp'],
                        'price': current_candle['close'],
                        'price_change_1m': round(price_change_1m, 2),
                        'price_change_5m': round(price_change_5m, 2),
                        'volume_ratio': round(volume_ratio, 2),
                        'volume_usdt': round(current_volume_usdt, 2),
                        'rsi': round(rsi, 2) if rsi else None,
                        'momentum': round(momentum, 2),
                        'buy_pressure': round(buy_pressure, 2),
                        'confidence': confidence,
                        'is_new_candle': is_new_candle,
                    }

                    # Đánh dấu đã pump
                    self.recent_pumps[symbol] = {
                        'timestamp': time.time(),
                        'candle_timestamp': current_candle['timestamp']
                    }

                    # Gửi thông báo
                    await self.notify_pump(pump_signal)

                    # Callback để vào lệnh
                    if self.on_pump_detected:
                        asyncio.create_task(self.on_pump_detected(pump_signal))

        except Exception as e:
            self.log.e(self.tag, f"Error analyzing pump for {symbol}: {e}\n{traceback.format_exc()}")

    def calculate_price_change_realtime(self, candles, periods):
        """Tính % thay đổi giá trong N nến (realtime)"""
        if len(candles) < periods + 1:
            return 0

        old_price = candles[-(periods + 1)]['close']
        new_price = candles[-1]['close']

        if old_price == 0:
            return 0

        return ((new_price - old_price) / old_price) * 100

    def calculate_volume_spike_realtime(self, candles):
        """
        Tính tỷ lệ volume hiện tại so với trung bình
        Normalize volume theo thời gian của nến hiện tại
        """
        if len(candles) < 10:
            return 1.0

        current_candle = candles[-1]

        # Tính thời gian của nến hiện tại (giây)
        current_time = int(time.time())
        candle_start_time = current_candle['timestamp']
        elapsed_seconds = current_time - candle_start_time

        # Normalize volume theo tỷ lệ thời gian (giả sử nến 1 phút = 60 giây)
        candle_interval_seconds = 60  # 1 minute
        time_ratio = elapsed_seconds / candle_interval_seconds if elapsed_seconds > 0 else 1
        time_ratio = min(time_ratio, 1.0)  # Cap tối đa 1.0

        # Volume chuẩn hóa (ước tính volume full nến)
        normalized_current_volume = current_candle['volume'] / time_ratio if time_ratio > 0.1 else current_candle[
            'volume']

        # Volume trung bình của các nến hoàn chỉnh trước đó
        avg_volume = np.mean([c['volume'] for c in candles[-20:-1]])

        if avg_volume == 0:
            return 1.0

        volume_ratio = normalized_current_volume / avg_volume

        return volume_ratio

    def has_recent_pump(self, symbol):
        """
        Kiểm tra xem symbol đã pump gần đây chưa
        Xem 20 nến gần đây có nến tăng mạnh + volume cao không
        """
        candles = list(self.candle_history[symbol]["Min1"])

        if len(candles) < self.pump_lookback_candles + 1:
            return False

        # Kiểm tra 20 nến gần đây (không tính nến hiện tại)
        recent_candles = candles[-(self.pump_lookback_candles + 1):-1]

        for i, candle in enumerate(recent_candles):
            # Tính price change của nến này so với nến trước đó
            if i > 0:
                prev_candle = recent_candles[i - 1]
                price_change = ((candle['close'] - prev_candle['close']) / prev_candle['close']) * 100

                # Tính volume ratio
                if i >= 10:
                    avg_volume = np.mean([c['volume'] for c in recent_candles[i - 10:i]])
                    volume_ratio = candle['volume'] / avg_volume if avg_volume > 0 else 1
                else:
                    volume_ratio = 1

                # Nếu đã có pump mạnh gần đây
                if (price_change >= self.config['recent_pump_price_threshold'] and
                        volume_ratio >= self.config['recent_pump_volume_threshold']):
                    self.log.d(self.tag,
                               f"🔍 {symbol} already pumped recently "
                               f"(price: +{price_change:.1f}%, volume: {volume_ratio:.1f}x) "
                               f"- skipping"
                               )
                    return True

        return False

    def is_in_cooldown(self, symbol):
        """Kiểm tra xem symbol có đang trong cooldown không"""
        if symbol not in self.recent_pumps:
            return False

        elapsed = time.time() - self.recent_pumps[symbol]['timestamp']
        return elapsed < self.pump_cooldown_seconds

    def calculate_rsi(self, candles, period=14):
        """Tính RSI (Relative Strength Index)"""
        if len(candles) < period + 1:
            return None

        prices = [c['close'] for c in candles[-(period + 1):]]
        deltas = np.diff(prices)

        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)

        if avg_loss == 0:
            return 100

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def calculate_momentum(self, candles):
        """Tính momentum (tốc độ thay đổi giá)"""
        if len(candles) < 5:
            return 0

        # So sánh momentum hiện tại với momentum trước đó
        recent_change = candles[-1]['close'] - candles[-3]['close']
        previous_change = candles[-3]['close'] - candles[-5]['close']

        if previous_change == 0:
            return 0

        return (recent_change / abs(previous_change)) if previous_change != 0 else 0

    def calculate_buy_pressure(self, candles):
        """Tính áp lực mua (% nến xanh trong 10 nến gần nhất)"""
        if len(candles) < 10:
            return 0

        recent_candles = candles[-10:]
        green_candles = sum(1 for c in recent_candles if c['close'] > c['open'])

        return (green_candles / len(recent_candles)) * 100

    def calculate_confidence(self, price_1m, price_5m, volume_ratio, rsi, momentum, buy_pressure):
        """Tính confidence score (0-100)"""
        confidence = 0

        # 1. Price change (0-30 điểm)
        price_score = min(30, (price_1m / self.config['price_increase_1m']) * 15)
        if price_5m >= self.config['price_increase_5m']:
            price_score += 15
        confidence += price_score

        # 2. Volume (0-25 điểm)
        volume_score = min(25, (volume_ratio / self.config['volume_spike_multiplier']) * 25)
        confidence += volume_score

        # 3. RSI (0-15 điểm)
        if rsi and rsi >= self.config['rsi_overbought']:
            confidence += 15
        elif rsi and rsi >= 60:
            confidence += 10

        # 4. Momentum (0-15 điểm)
        if momentum >= self.config['momentum_threshold']:
            confidence += 15
        elif momentum >= 1.0:
            confidence += 10

        # 5. Buy pressure (0-15 điểm)
        if buy_pressure >= 80:
            confidence += 15
        elif buy_pressure >= 60:
            confidence += 10

        return min(100, round(confidence))

    async def notify_pump(self, signal):
        """Gửi thông báo pump qua Telegram"""
        candle_status = "🆕 NEW CANDLE" if signal['is_new_candle'] else "📊 REALTIME"

        message = f"""
🚀 PUMP DETECTED! {candle_status} 🚀
━━━━━━━━━━━━━━━━━━━━━━━━
📊 Coin: {signal['symbol']}
💰 Price: ${signal['price']:.6f}
📈 Change 1m: +{signal['price_change_1m']}%
📈 Change 5m: +{signal['price_change_5m']}%
📊 Volume Ratio: {signal['volume_ratio']}x
💵 Volume: ${signal['volume_usdt']:,.0f}
🎯 RSI: {signal['rsi']}
⚡ Momentum: {signal['momentum']}
💪 Buy Pressure: {signal['buy_pressure']}%
🔥 Confidence: {signal['confidence']}%
⏰ Time: {signal['timestamp']}
━━━━━━━━━━━━━━━━━━━━━━━━
⚠️ Khuyến nghị: Kiểm tra kỹ trước khi trade!
        """

        self.log.i(self.tag, message)
        await self.tele_message.send_message(message, self.chat_id)