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
        # 타임아웃을 설정하여 봇이 무한 대기하는 것을 방지합니다.
        self.client = httpx.AsyncClient(timeout=10.0) 
        self.pending_cancellations: set[str] = set()
        
        # 초기 인증 정보 설정
        self.creds = {
            "key": self.settings.polymarket_builder_api_key,
            "secret": self.settings.polymarket_builder_secret,
            "passphrase": self.settings.polymarket_builder_passphrase
        }
        self.safe_address = self.settings.public_address

    async def initialize(self):
        """[핵심] TypeScript의 initializeClobClient 로직을 수행합니다."""
        logger.info("initializing_clob_auth")
        
        # 1. API 키가 없으면 자동 발급 (TS의 createApiKey)
        if not self.creds["key"]:
            await self._auto_create_api_keys()

        # 2. Safe 주소가 없으면 폴리마켓 프로필에서 자동 조회
        if not self.safe_address:
            await self._auto_fetch_safe_address()

    async def _auto_create_api_keys(self):
        """지갑 서명을 사용하여 서버로부터 새 API 키를 받아옵니다."""
        try:
            ts = int(time.time())
            # 폴리마켓 표준 인증 메시지
            msg = f"Polymarket API Authentication: {ts}"
            sig = self.order_signer.sign_text(msg)
            
            payload = {
                "address": self.order_signer.get_address(),
                "timestamp": ts,
                "signature": sig
            }
            
            resp = await self.client.post(
                f"{self.settings.polymarket_api_url}/auth/api-key",
                json=payload
            )
            
            if resp.status_code == 200:
                data = resp.json()
                self.creds = {
                    "key": data["apiKey"],
                    "secret": data["secret"],
                    "passphrase": data["passphrase"]
                }
                logger.info("✅ API 키 자동 발급 성공")
            else:
                logger.error(f"❌ API 키 발급 실패: {resp.text}")
        except Exception as e:
            logger.error(f"❌ API 초기화 중 오류: {e}")

    async def _auto_fetch_safe_address(self):
        """서버 프로필 조회를 통해 사용자의 Safe 주소를 찾습니다."""
        try:
            eoa = self.order_signer.get_address()
            resp = await self.client.get(f"https://gamma-api.polymarket.com/profiles?wallet={eoa}")
            if resp.status_code == 200:
                data = resp.json()
                if data and len(data) > 0:
                    self.safe_address = data[0].get("proxyAddress")
                    logger.info(f"✅ Safe 주소 자동 매칭: {self.safe_address}")
        except Exception as e:
            logger.error(f"❌ Safe 주소 조회 실패: {e}")

    def _get_auth_headers(self) -> dict:
        """[수정] 동적으로 발급된 Creds를 헤더에 적용"""
        return {
            "POLY-API-KEY": self.creds["key"],
            "POLY-API-SECRET": self.creds["secret"],
            "POLY-API-PASSPHRASE": self.creds["passphrase"],
            "Content-Type": "application/json"
        }

    def _format_order(self, order: dict[str, Any]) -> dict[str, Any]:
        formatted = order.copy()
        formatted["price"] = f"{round(float(order['price']), 3):.3f}"
        formatted["size"] = f"{round(float(order['size']), 2):.2f}"
        return formatted   

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """[최종] 정밀한 페이로드 구성 및 서명 전송"""
        try:
            is_buy = order["side"] == "BUY"
            size = float(order["size"])
            price = float(order["price"])
            
            raw_shares = int(round(size * 10**6))
            raw_usdc = int(round(size * price, 6) * 10**6)
            
            # EIP-712 규격에 맞춘 데이터 구성
            order_data = {
                "maker": self.safe_address,
                "taker": "0x0000000000000000000000000000000000000000",
                "tokenId": int(order["token_id"]),
                "makerAmount": int(raw_usdc if is_buy else raw_shares),
                "takerAmount": int(raw_shares if is_buy else raw_usdc),
                "side": 0 if is_buy else 1,
                "feeRateBps": 0,
                "nonce": 0,
                "signer": self.order_signer.get_address(),
                "expiration": int(time.time()) + 3600,
                "salt": int(time.time()),
                "signatureType": 2 # Safe Proxy 지갑
            }
            
            # 서명 생성
            signature = self.order_signer.sign_order(order_data)
            
            # 최종 페이로드
            final_payload = {
                **order_data,
                "tokenId": str(order_data["tokenId"]),
                "makerAmount": str(order_data["makerAmount"]),
                "takerAmount": str(order_data["takerAmount"]),
                "signature": signature
            }
            
            response = await self.client.post(
                f"{self.settings.polymarket_api_url}/order",
                json=final_payload,
                headers=self._get_auth_headers(),
            )
            response.raise_for_status()
            
            result = response.json()
            logger.info("order_placed_success", order_id=result.get("id"))
            return result
        except Exception as e:
            logger.error("order_placement_failed", error=str(e))
            raise

    async def close(self):
        await self.client.aclose()

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


