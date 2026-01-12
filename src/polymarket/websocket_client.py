# src/polymarket/websocket_client.py
from __future__ import annotations
import asyncio
import json
from typing import Any, Callable
import structlog
import websockets
from src.config import Settings

logger = structlog.get_logger(__name__)

class PolymarketWebSocketClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ws_url = settings.polymarket_ws_url.strip().replace('"', '').replace("'", "")
        self.websocket = None
        self.message_handlers = {}
        self.running = False
        self._subscriptions = {} 
        self._connect_lock = asyncio.Lock()

    def register_handler(self, message_type: str, handler: Callable):
        self.message_handlers[message_type] = handler

    def _is_websocket_open(self) -> bool:
        """현재 웹소켓이 활성화 상태인지 확인 (다양한 라이브러리 버전 대응)"""
        if self.websocket is None:
            return False
        
        # 1. websockets 표준 속성 확인
        if hasattr(self.websocket, 'open'):
            return self.websocket.open
            
        # 2. 상태 문자열 확인 (CONNECTING/OPEN 상태라면 닫힌 것으로 간주하지 않음)
        state = str(getattr(self.websocket, 'state', '')).upper()
        return "OPEN" in state or "CONNECTING" in state

    async def _send_subscribe(self, message: dict):
        """구독 정보를 캐싱하고, 안전하게 전송합니다."""
        channel = message.get('channel') or (message.get('channels')[0] if message.get('channels') else 'unknown')
        market = message.get('market') or (message.get('assets_ids')[0] if message.get('assets_ids') else '')
        sub_key = f"{channel}:{market}"
        self._subscriptions[sub_key] = message

        # 연결이 되어 있지 않거나 끊겼다면 connect() 호출
        if not self._is_websocket_open():
            await self.connect()
            # connect() 내부에서 이미 모든 구독 정보를 재전송하므로 여기서 종료
            return

        # 이미 열린 상태라면 이 메시지만 전송
        try:
            await self.websocket.send(json.dumps(message))
        except Exception as e:
            logger.debug("subscribe_send_failed_retrying", error=str(e))
            await self.connect()

    async def connect(self):
        """웹소켓 연결 및 재구독 (Lock으로 폭풍 방지)"""
        async with self._connect_lock:
            # Lock 획득 후 다시 한번 상태 확인 (중복 연결 방지)
            if self._is_websocket_open():
                return

            try:
                # [개선] 기존 연결이 유효하지 않을 때만 명시적으로 닫기
                if self.websocket:
                    try: 
                        await asyncio.wait_for(self.websocket.close(), timeout=1)
                    except: 
                        pass

                logger.info("attempting_websocket_connection", url=self.ws_url)
                self.websocket = await websockets.connect(
                    self.ws_url, 
                    ping_interval=20, 
                    ping_timeout=20
                )
                self.running = True
                
                # 핸드셰이크 직후 서버 안정을 위해 대기
                await asyncio.sleep(0.5)
                
                # 저장된 모든 구독 정보 일괄 전송
                if self._subscriptions:
                    logger.info("resubscribing_to_all_channels", count=len(self._subscriptions))
                    for sub_msg in self._subscriptions.values():
                        await self.websocket.send(json.dumps(sub_msg))
                
                logger.info("websocket_connected_and_resubscribed")
            except Exception as e:
                logger.error("websocket_connection_failed", error=str(e))
                raise

    async def subscribe_orderbook(self, market_id: str):
        message = {
            "type": "subscribe",
            "assets_ids": [market_id],
            "channels": ["order_book_l2"],
        }
        await self._send_subscribe(message)
        logger.info("orderbook_subscribed", market_id=market_id)

    async def subscribe_user(self, user_address: str):
        message = {
            "type": "subscribe", 
            "channel": "user", 
            "user": user_address
        }
        await self._send_subscribe(message)
        logger.info("user_channel_subscribed", address=user_address)

    async def listen(self):
        """메시지 수신 루프"""
        if not self._is_websocket_open():
            await self.connect()
            
        while self.running:
            try:
                message = await self.websocket.recv()
                if not message: continue
                
                try:
                    data = json.loads(message)
                except json.JSONDecodeError: continue

                m_type = data.get("type") or data.get("event_type")
                if m_type in self.message_handlers:
                    await self.message_handlers[m_type](data)
            
            except Exception as e:
                if self.running:
                    logger.warning("websocket_listen_error_reconnecting", error=str(e))
                    await asyncio.sleep(2)
                    try: await self.connect()
                    except: continue

    async def close(self):
        self.running = False
        if self.websocket:
            await self.websocket.close()