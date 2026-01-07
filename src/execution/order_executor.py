# src/execution/order_executor.py
from __future__ import annotations

import asyncio
import time
import json
import logging
from typing import Any, List, Optional
import httpx
import structlog

from src.config import Settings
from src.polymarket.order_signer import OrderSigner

logger = structlog.get_logger(__name__)

class OrderExecutor:
    def __init__(self, settings: Settings, order_signer: OrderSigner):
        self.settings = settings
        self.order_signer = order_signer
        self.client = httpx.AsyncClient(
            base_url=settings.polymarket_api_url,
            timeout=10.0
        )
        self.creds = {}
        self.pending_cancellations = set()
        
        # [수정 1] safe_address 초기화 (이 부분이 없어서 에러가 났었습니다)
        # 기본값으로 설정 파일의 지갑 주소를 사용합니다.
        self.safe_address = self.settings.public_address

    async def initialize(self):
        """API 클라이언트 초기화 및 인증 수행"""
        logger.info("initializing_clob_auth")
        
        # 1. API 키 발급 또는 조회
        if not self.creds:
            await self._auto_create_api_keys()

        # 2. Proxy(Safe) 주소 자동 조회
        # Gnosis Safe 사용자의 경우 실제 주문자가 Proxy 주소가 되어야 하므로 이를 찾습니다.
        await self._auto_fetch_safe_address()

    async def _auto_create_api_keys(self):
        """
        [수정 2] 지갑 서명을 사용하여 API 키를 생성(Create)하거나, 
        실패 시 기존 키를 조회(Derive)하는 통합 로직입니다.
        """
        try:
            ts = int(time.time())
            nonce = 0
            
            # 서명 생성 (체크섬 주소 사용)
            sig = self.order_signer.sign_clob_auth_message(ts, nonce)
            address_checksum = self.order_signer.get_address()
            
            # 헤더 구성 (대문자 키 이름 사용 - 중요)
            headers = {
                "POLY_ADDRESS": address_checksum,
                "POLY_TIMESTAMP": str(ts),
                "POLY_NONCE": str(nonce),
                "POLY_SIGNATURE": sig,
                "Content-Type": "application/json"
            }
            
            # [시도 1] API 키 생성 (Create)
            create_url = f"{self.settings.polymarket_api_url}/auth/api-key"
            # Body를 비워서 보냅니다.
            resp = await self.client.post(create_url, headers=headers) 
            
            if resp.status_code == 200:
                data = resp.json()
                logger.info("✅ API 키 생성 성공")
            else:
                # 생성 실패 시 (400 Bad Request 등) -> 조회 시도
                logger.warning(f"⚠️ API 키 생성 실패 (Status {resp.status_code}). 기존 키 조회(Derive)를 시도합니다.")
                
                # [시도 2] 기존 API 키 조회 (Derive)
                derive_url = f"{self.settings.polymarket_api_url}/auth/derive-api-key"
                resp = await self.client.get(derive_url, headers=headers)
                
                if resp.status_code == 200:
                    data = resp.json()
                    logger.info("✅ 기존 API 키 조회(Derive) 성공")
                else:
                    logger.error(f"❌ API 키 조회 실패: {resp.text}")
                    return

            # 발급/조회된 키 적용
            self.creds = {
                "key": data["apiKey"],
                "secret": data["secret"],
                "passphrase": data["passphrase"]
            }
            logger.info(f"🔑 API Key applied: {self.creds['key'][:10]}...")

        except Exception as e:
            logger.error(f"❌ API 초기화 중 오류: {e}")

    async def _auto_fetch_safe_address(self):
        """서버 프로필 조회를 통해 사용자의 Safe(Proxy) 주소를 찾습니다."""
        try:
            eoa = self.order_signer.get_address()
            # 폴리마켓 감마 API를 통해 Proxy 주소 조회
            resp = await self.client.get(f"https://gamma-api.polymarket.com/profiles?wallet={eoa}")
            
            if resp.status_code == 200:
                data = resp.json()
                # 데이터가 리스트 형태이며 proxyAddress가 존재하는지 확인
                if isinstance(data, list) and len(data) > 0:
                    proxy = data[0].get("proxyAddress")
                    if proxy:
                        self.safe_address = proxy
                        logger.info(f"✅ Safe(Proxy) 주소 자동 매칭: {self.safe_address}")
                    else:
                        logger.info("ℹ️ Proxy 주소가 없어 EOA 주소를 사용합니다.")
        except Exception as e:
            logger.warning(f"⚠️ Safe 주소 조회 실패 (기본 주소 사용): {e}")

    def _get_auth_headers(self) -> dict:
        """API 요청용 인증 헤더 생성"""
        if not self.creds:
            return {}
        return {
            "POLY-API-KEY": self.creds.get("key", ""),
            "POLY-API-SECRET": self.creds.get("secret", ""),
            "POLY-API-PASSPHRASE": self.creds.get("passphrase", ""),
            "Content-Type": "application/json"
        }

    async def place_order(self, order: dict[str, Any]) -> dict[str, Any]:
        """주문 서명 및 전송"""
        try:
            # 안전장치: safe_address가 없으면 에러 방지를 위해 서명자 주소 사용
            maker_address = self.safe_address if self.safe_address else self.order_signer.get_address()
            
            is_buy = order["side"] == "BUY"
            size = float(order["size"])
            price = float(order["price"])
            token_id = int(order["token_id"])
            
            # 폴리마켓은 10^6 단위 사용 (USDC)
            raw_shares = int(size * 10**6) 
            raw_usdc = int(size * price * 10**6)
            
            # EIP-712 주문 데이터 구성
            order_data = {
                "maker": maker_address,
                "taker": "0x0000000000000000000000000000000000000000",
                "tokenId": token_id,
                "makerAmount": raw_usdc if is_buy else raw_shares,
                "takerAmount": raw_shares if is_buy else raw_usdc,
                "side": 0 if is_buy else 1,
                "feeRateBps": 0,
                "nonce": 0, 
                "signer": self.order_signer.get_address(),
                "expiration": int(time.time()) + 300, # 5분 유효
                "salt": int(time.time()),
                "signatureType": 2 if self.safe_address and self.safe_address != self.order_signer.get_address() else 0
            }
            
            # 서명 생성
            signature = self.order_signer.sign_order(order_data)
            
            # 최종 전송 페이로드 (문자열 변환 필수)
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
            return {}

    async def cancel_order(self, order_id: str) -> bool:
        """개별 주문 취소"""
        try:
            if order_id in self.pending_cancellations:
                return True
            
            self.pending_cancellations.add(order_id)
            
            response = await self.client.delete(
                f"{self.settings.polymarket_api_url}/order/{order_id}",
                headers=self._get_auth_headers()
            )
            
            # 이미 취소된 주문(404)도 성공으로 간주
            if response.status_code in [200, 404]:
                logger.info("order_cancelled", order_id=order_id)
                return True
                
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error("order_cancellation_failed", order_id=order_id, error=str(e))
            self.pending_cancellations.discard(order_id)
            return False

    async def cancel_all_orders(self, market_id: str) -> int:
        """특정 마켓의 모든 주문 일괄 취소"""
        try:
            response = await self.client.delete(
                f"{self.settings.polymarket_api_url}/orders",
                params={"market": market_id},
                headers=self._get_auth_headers()
            )
            response.raise_for_status()
            
            # 응답에서 취소된 수 확인 (없으면 0)
            data = response.json()
            cancelled_count = len(data.get("data", [])) if "data" in data else 0
            
            logger.info("all_orders_cancelled", market_id=market_id, count=cancelled_count)
            self.pending_cancellations.clear()
            return cancelled_count
        except Exception as e:
            logger.error("cancel_all_failed", error=str(e))
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
                headers=self._get_auth_headers()
            )
            response.raise_for_status()
            
            self.pending_cancellations.clear()
            logger.info("batch_cancel_success", count=len(order_ids))
            return len(order_ids)
        except Exception as e:
            logger.error("batch_cancel_failed", error=str(e))
            return 0

    async def close(self):
        await self.client.aclose()