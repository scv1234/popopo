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
        self._subscriptions = {} # 모든 구독 정보를 저장

    def register_handler(self, message_type: str, handler: Callable):
        self.message_handlers[message_type] = handler

    async def _send_subscribe(self, message: dict):
        if not self.websocket:
            await self.connect()

        # 채널 키 생성
        channel = message.get('channel') or (message.get('channels')[0] if message.get('channels') else 'unknown')
        market = message.get('market') or (message.get('assets_ids')[0] if message.get('assets_ids') else '')
        sub_key = f"{channel}:{market}"
    
        self._subscriptions[sub_key] = message
    
        # [수정] hasattr과 'in' 연산자를 사용하여 모든 버전의 websockets 라이브러리에 대응
        is_connected = False
        if self.websocket:
            if hasattr(self.websocket, 'closed'):
                is_connected = not self.websocket.closed
            elif hasattr(self.websocket, 'open'):
                is_connected = self.websocket.open
            else:
                # 최신 버전의 경우 state 속성 검사 (contains 대신 'in' 사용)
                state_str = str(getattr(self.websocket, 'state', ''))
                is_connected = "OPEN" in state_str

        if is_connected:
            try:
                await self.websocket.send(json.dumps(message))
            except Exception as e:
                logger.error("websocket_send_failed", error=str(e))
                await self.connect()
                await self.websocket.send(json.dumps(message))
        else:
            logger.warning("websocket_not_ready_reconnecting")
            await self.connect()
            if self.websocket:
                await self.websocket.send(json.dumps(message))

    async def connect(self):
        try:
            if self.websocket: await self.websocket.close()
            self.websocket = await websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20)
            self.running = True
            
            # 저장된 모든 채널(오더북 포함) 재구독
            if self._subscriptions:
                for sub_msg in self._subscriptions.values():
                    await self.websocket.send(json.dumps(sub_msg))
            logger.info("websocket_connected_and_resubscribed")
        except Exception as e:
            logger.error("websocket_connection_failed", error=str(e))
            raise

    async def subscribe_orderbook(self, market_id: str):
        """저장 로직을 위해 _send_subscribe를 사용하도록 수정"""
        message = {
            "type": "subscribe",
            "assets_ids": [market_id],
            "channels": ["order_book_l2"],
        }
        await self._send_subscribe(message)
        logger.info("orderbook_subscribed_ts_style", market_id=market_id)

    async def subscribe_trades(self, market_id: str):
        """체결 내역 채널 구독"""
        message = {
            "type": "subscribe",
            "channel": "trades",
            "market": market_id,
        }
        await self._send_subscribe(message)
        logger.info("trades_subscribed", market_id=market_id)

    async def subscribe_user(self, user_address: str):
        """사용자 개인 이벤트 채널 구독"""
        message = {
            "type": "subscribe", 
            "channel": "user", 
            "user": user_address
        }
        await self._send_subscribe(message)
        logger.info("user_channel_subscribed", address=user_address)

    async def listen(self):
        await self.connect()
        while True: # 루프가 절대 죽지 않도록 설정
            try:
                message = await self.websocket.recv()
                data = json.loads(message)
                m_type = data.get("type") or data.get("event_type")
                if m_type in self.message_handlers:
                    await self.message_handlers[m_type](data)
            except Exception as e:
                logger.warning("websocket_error_reconnecting", error=str(e))
                await asyncio.sleep(5)
                try: await self.connect()
                except: continue

    async def close(self):
        self.running = False
        self._subscriptions.clear() # 종료 시 구독 정보 삭제
        if self.websocket:
            await self.websocket.close()
            logger.info("websocket_closed")