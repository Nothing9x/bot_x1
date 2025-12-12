from abc import ABC, abstractmethod

class IPositionSocket(ABC):
    @abstractmethod
    async def start_position_socket(self):
        """Bắt đầu kết nối socket"""
        pass

    @abstractmethod
    async def stop_position_socket(self):
        """Dừng socket và hủy các task đang chạy"""
        pass

    @abstractmethod
    async def connect(self):
        """Kết nối lại khi socket mất kết nối"""
        pass

    @abstractmethod
    async def send_ping(self):
        """Gửi ping định kỳ để giữ kết nối"""
        pass

    @abstractmethod
    async def listen(self):
        """Lắng nghe dữ liệu từ WebSocket"""
        pass

    @abstractmethod
    async def _auth(self):
        """Thực hiện xác thực người dùng với API key/secret"""
        pass

    @abstractmethod
    async def subscribe(self):
        """Đăng ký các sự kiện cần lắng nghe từ server"""
        pass
