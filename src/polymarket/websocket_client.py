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
        self.message_handlers: dict[str, Callable] = {}
        self.running = False
        # [수정] 채널별로 최신 구독 메시지만 관리하기 위해 dict로 변경
        self._subscriptions: dict[str, str] = {} 

    def register_handler(self, message_type: str, handler: Callable):
        self.message_handlers[message_type] = handler

    async def _send_subscribe(self, message: dict):
        """[추가] 메시지를 전송하고 구독 목록에 저장하는 헬퍼 함수"""
        if not self.websocket or not self.running:
            await self.connect()
            
        msg_str = json.dumps(message)
        # 채널(및 마켓)을 키로 사용하여 중복 구독 방지 및 최신화
        # 예: "l2_book:market_id" 또는 "user"
        sub_key = f"{message['channel']}:{message.get('market', '')}"
        self._subscriptions[sub_key] = msg_str
        
        await self.websocket.send(msg_str)

    async def connect(self):
        try:
            # ping_interval/timeout 설정으로 연결 유지력 강화
            self.websocket = await websockets.connect(
                self.ws_url,
                ping_interval=20,
                ping_timeout=20
            )
            self.running = True
            
            # [수정] 저장된 구독 정보가 있다면 재연결 즉시 복구
            if self._subscriptions:
                logger.info("resubscribing_to_previous_channels", count=len(self._subscriptions))
                for sub_msg in self._subscriptions.values():
                    await self.websocket.send(sub_msg)
            
            logger.info("websocket_connected_successfully")
        except Exception as e:
            logger.error("websocket_connection_failed", error=str(e))
            raise

    async def subscribe_orderbook(self, market_id: str):
        if not self.websocket:
            await self.connect()

        # TS 코드와 동일한 규격으로 변경
        message = {
            "type": "subscribe",
            "assets_ids": [market_id],  # market -> assets_ids (리스트 형태)
            "channels": ["order_book_l2"], # l2_book -> order_book_l2
        }
        await self.websocket.send(json.dumps(message))
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
        if not self.websocket:
            await self.connect()

        while self.running:
            try:
                message = await self.websocket.recv()
                
                # [수정] 빈 메시지 또는 공백만 있는 메시지 무시
                if not message or not message.strip():
                    continue

                data = json.loads(message.strip())

                message_type = data.get("type") or data.get("event_type") # 규격 대응
                if message_type and message_type in self.message_handlers:
                    await self.message_handlers[message_type](data)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("websocket_connection_closed_reconnecting")
                self.running = False # 잠시 중단 후 connect에서 다시 True가 됨
                await asyncio.sleep(5)
                try:
                    await self.connect()
                except Exception:
                    continue # 재연결 실패 시 다음 루프에서 다시 시도
            except json.JSONDecodeError:
                # JSON 형식이 아닌 메시지는 로그만 남기고 무시
                continue
            except Exception as e:
                logger.error("websocket_listen_error", error=str(e))
                await asyncio.sleep(1)

    async def close(self):
        self.running = False
        self._subscriptions.clear() # 종료 시 구독 정보 삭제
        if self.websocket:
            await self.websocket.close()
            logger.info("websocket_closed")