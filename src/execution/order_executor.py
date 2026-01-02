from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import structlog

from src.config import Settings
from src.polymarket.order_signer import OrderSigner

logger = structlog.get_logger(__name__)


class OrderExecutor:
    def __init__(self, settings: Settings, order_signer: OrderSigner):
        self.settings = settings
        self.order_signer = order_signer
        self.client = httpx.AsyncClient(timeout=30.0)
        self.pending_cancellations: set[str] = set()

    def _get_auth_headers(self) -> dict:
        """[추가] 폴리마켓 CLOB 인증 헤더 생성"""
        key = self.settings.polymarket_builder_api_key
        if not key:
            logger.error("❌ API_KEY_IS_EMPTY: .env 파일을 확인하세요.")
        return {
            "POLY-API-KEY": self.settings.polymarket_builder_api_key,
            "POLY-API-SECRET": self.settings.polymarket_builder_secret,
            "POLY-API-PASSPHRASE": self.settings.polymarket_builder_passphrase,
            "Content-Type": "application/json"
        }    

    def _format_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """
        Polymarket CLOB 규격에 맞게 가격과 수량을 포맷팅합니다.
        - 가격: 소수점 3자리 (Tick Size: 0.001)
        - 수량: 소수점 2자리 (Step Size: 0.01)
        """
        formatted = order.copy()
        formatted["price"] = f"{round(float(order['price']), 3):.3f}"
        formatted["size"] = f"{round(float(order['size']), 2):.2f}"
        return formatted    

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """주문을 포맷팅하고 서명하여 시장에 제출합니다."""
        try:
            # 1. 수량 및 가격 정밀도 조정 (API 거절 방지)
            order = self._format_order(order)
            
            timestamp = int(time.time() * 1000)
            order["time"] = timestamp
            order["salt"] = str(int(time.time()))
            
            # 2. 서명 및 주소 추가
            signature = self.order_signer.sign_order(order)
            order["signature"] = signature
            order["maker"] = self.order_signer.get_address()
            
            response = await self.client.post(
                f"{self.settings.polymarket_api_url}/order",
                json=order,
                headers=self._get_auth_headers(), # 인증 헤더 적용
            )
            response.raise_for_status()
            
            result = response.json()
            logger.info("order_placed_success", 
                        order_id=result.get("id"), 
                        side=order.get("side"), 
                        price=order["price"], 
                        size=order["size"])
            return result
        except Exception as e:
            logger.error("order_placement_failed", error=str(e), order=order)
            raise

    async def cancel_order(self, order_id: str) -> bool:
        """개별 주문 취소 (방어 로직 실시간 가동용)"""
        try:
            # 중복 취소 방지
            if self.settings.batch_cancellations and order_id in self.pending_cancellations:
                return True
            
            self.pending_cancellations.add(order_id)
            
            response = await self.client.delete(
                f"{self.settings.polymarket_api_url}/order/{order_id}",
                headers=self._get_auth_headers() # 인증 헤더 적용
            )
            response.raise_for_status()
            
            logger.info("order_cancelled_confirmed", order_id=order_id)
            return True
        except Exception as e:
            logger.error("order_cancellation_failed", order_id=order_id, error=str(e))
            self.pending_cancellations.discard(order_id)
            return False

    async def cancel_all_orders(self, market_id: str) -> int:
        """
        [Hard-Limit 방어용] 특정 마켓의 모든 주문을 일괄 취소합니다.
        비상 상황 시 원금 보호를 위해 가장 빠르게 작동해야 합니다.
        """
        try:
            response = await self.client.delete(
                f"{self.settings.polymarket_api_url}/orders",
                params={"market": market_id},
                headers = self._get_auth_headers()
            )
            response.raise_for_status()
            
            cancelled = response.json().get("cancelled", 0)
            logger.info("emergency_all_orders_cancelled", market_id=market_id, count=cancelled)
            self.pending_cancellations.clear()
            return cancelled
        except Exception as e:
            logger.error("cancel_all_orders_failed", market_id=market_id, error=str(e))
            return 0

    async def batch_cancel_orders(self, order_ids: list[str]) -> int:
        """여러 주문을 한 번에 취소하여 API 호출 횟수를 절약합니다."""
        if not order_ids:
            return 0

        if not self.settings.batch_cancellations:
            # 순차 취소
            tasks = [self.cancel_order(oid) for oid in order_ids]
            results = await asyncio.gather(*tasks)
            return sum(1 for r in results if r)
        
        try:
            response = await self.client.post(
                f"{self.settings.polymarket_api_url}/orders/cancel",
                json={"orderIds": order_ids},
                headers=self._get_auth_headers() # 인증 헤더 적용
            )
            response.raise_for_status()
            
            self.pending_cancellations.clear()
            logger.info("batch_cancel_success", count=len(order_ids))
            return len(order_ids)
        except Exception as e:
            logger.error("batch_cancel_failed", error=str(e))
            return 0

    async def close(self):
        """HTTP 클라이언트를 종료합니다."""
        await self.client.aclose()

