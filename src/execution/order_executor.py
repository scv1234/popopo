# src/execution/order_executor.py
from __future__ import annotations

import asyncio
import time
import json
import hmac
import hashlib
import base64
from typing import Any, Dict, Optional, List
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
            timeout=15.0
        )
        self.creds = {}
        self.pending_cancellations = set()
        # 설정된 public_address가 있으면 우선 사용
        self.safe_address = self.settings.public_address

    async def initialize(self):
        """API 클라이언트 초기화 및 인증 수행"""
        logger.info("initializing_clob_auth")
        
        if self.settings.polymarket_builder_api_key:
            self.creds = {
                "key": self.settings.polymarket_builder_api_key,
                "secret": self.settings.polymarket_builder_secret,
                "passphrase": self.settings.polymarket_builder_passphrase
            }
        
        if not self.creds:
            await self._auto_create_api_keys()

        # EOA 주소의 Proxy(Safe) 주소를 자동으로 찾음
        await self._auto_fetch_safe_address()

    async def _auto_create_api_keys(self):
        """[공식 문서] API 키 생성 및 조회 (L3 Auth)"""
        try:
            ts = int(time.time())
            nonce = 0
            sig = self.order_signer.sign_clob_auth_message(ts, nonce)
            
            headers = {
                "POLY-ADDRESS": self.order_signer.get_address(),
                "POLY-SIGNATURE": sig,
                "POLY-TIMESTAMP": str(ts),
                "POLY-NONCE": str(nonce),
                "Content-Type": "application/json"
            }
            
            # 1. 기존 키가 있는지 먼저 확인 (GET)
            resp = await self.client.get("/auth/derive-api-key", headers=headers)
            if resp.status_code != 200:
                # 2. 없으면 신규 생성 (POST)
                resp = await self.client.post("/auth/api-key", headers=headers)
            
            if resp.status_code in [200, 201]:
                data = resp.json()
                self.creds = {
                    "key": data["apiKey"],
                    "secret": data["secret"],
                    "passphrase": data["passphrase"]
                }
                logger.info("✅ API Credentials Loaded", key=self.creds['key'][:8])
            else:
                logger.error("❌ Auth Failed", status=resp.status_code, text=resp.text)
        except Exception as e:
            logger.error("❌ API Initialization Error", error=str(e))

    async def _auto_fetch_safe_address(self):
        """Gamma API를 통한 Safe(Proxy) 주소 매칭"""
        try:
            eoa = self.order_signer.get_address()
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"https://gamma-api.polymarket.com/profiles?wallet={eoa}")
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        proxy = data[0].get("proxyAddress")
                        if proxy:
                            self.safe_address = proxy
                            logger.info("✅ Safe Address Found", safe=self.safe_address)
        except Exception as e:
            logger.warning("⚠️ Safe Address Fetch Failed", error=str(e))

    def _create_signed_headers(self, method: str, path: str, body: Optional[Dict] = None) -> dict:
        """[중요] HMAC-SHA256 서명 및 콤팩트 JSON 처리"""
        if not self.creds:
            return {}
            
        timestamp = str(int(time.time()))
        method = method.upper()
        
        # [핵심] 공식 문서는 공백 없는 JSON 문자열로 서명을 생성할 것을 요구함
        body_str = ""
        if body is not None:
            body_str = json.dumps(body, separators=(',', ':'))
        
        message = timestamp + method + path + body_str
        
        secret_bytes = base64.b64decode(self.creds["secret"])
        signature = base64.b64encode(
            hmac.new(secret_bytes, message.encode('utf-8'), hashlib.sha256).digest()
        ).decode('utf-8')

        return {
            "POLY-API-KEY": self.creds["key"],
            "POLY-API-SIGNATURE": signature,
            "POLY-API-TIMESTAMP": timestamp,
            "POLY-API-PASSPHRASE": self.creds["passphrase"],
            "Content-Type": "application/json"
        }

    async def place_order(self, order_params: dict[str, Any]) -> dict[str, Any]:
        """주문 전송 (EIP-712 서명 포함)"""
        try:
            maker_address = self.safe_address if self.safe_address else self.order_signer.get_address()
            is_buy = order_params["side"].upper() == "BUY"
            
            # 수량/가격 계산 (6 decimal)
            raw_size = int(float(order_params["size"]) * 1e6)
            raw_price = float(order_params["price"])
            
            if is_buy:
                maker_amount = int(raw_size * raw_price)
                taker_amount = raw_size
            else:
                maker_amount = raw_size
                taker_amount = int(raw_size * raw_price)

            order_data = {
                "maker": maker_address,
                "taker": "0x0000000000000000000000000000000000000000",
                "tokenId": str(order_params["token_id"]),
                "makerAmount": str(maker_amount),
                "takerAmount": str(taker_amount),
                "side": 0 if is_buy else 1,
                "feeRateBps": 0,
                "nonce": "0",
                "signer": self.order_signer.get_address(),
                "expiration": str(int(time.time()) + 3600), # 1시간 유효
                "salt": int(time.time() * 1000),
                "signatureType": 2 if self.safe_address and self.safe_address != self.order_signer.get_address() else 0
            }
            
            # 지갑 서명
            order_data["signature"] = self.order_signer.sign_order(order_data)
            
            # [수정] owner는 API Key가 아니라 Maker 주소여야 함
            final_payload = {
                "order": order_data,
                "owner": maker_address,
                "orderType": "GTC"
            }
            
            path = "/order"
            headers = self._create_signed_headers("POST", path, final_payload)
            
            # 전송 시에도 서명과 동일한 JSON 포맷 사용
            response = await self.client.post(
                path, 
                content=json.dumps(final_payload, separators=(',', ':')), 
                headers=headers
            )
            
            if response.status_code != 200:
                logger.error("❌ Place Order Failed", status=response.status_code, text=response.text)
                return {}
            
            result = response.json()
            logger.info("✅ Order Success", order_id=result.get("orderID"))
            return result
        except Exception as e:
            logger.error("❌ place_order Exception", error=str(e))
            return {}

    async def cancel_order(self, order_id: str) -> bool:
        """단일 주문 취소 (DELETE /order)"""
        try:
            body = {"orderID": order_id}
            path = "/order"
            headers = self._create_signed_headers("DELETE", path, body)
            
            response = await self.client.request(
                "DELETE", 
                path, 
                content=json.dumps(body, separators=(',', ':')), 
                headers=headers
            )
            return response.status_code == 200
        except Exception as e:
            logger.error("❌ Cancel Order Failed", error=str(e))
            return False

    async def cancel_all_orders(self) -> bool:
        """모든 주문 일괄 취소 (DELETE /orders)"""
        try:
            path = "/orders"
            # Body가 없는 경우 None 전달
            headers = self._create_signed_headers("DELETE", path, None)
            
            response = await self.client.request("DELETE", path, headers=headers)
            if response.status_code == 200:
                logger.info("✅ All Orders Cancelled")
                return True
            return False
        except Exception as e:
            logger.error("❌ Cancel All Failed", error=str(e))
            return False

    async def batch_cancel_orders(self, order_ids: List[str]) -> int:
        """여러 주문 ID를 지정하여 취소 (POST /orders/cancel)"""
        if not order_ids:
            return 0
        try:
            body = {"orderIds": order_ids}
            path = "/orders/cancel"
            headers = self._create_signed_headers("POST", path, body)
            
            response = await self.client.post(
                path, 
                content=json.dumps(body, separators=(',', ':')), 
                headers=headers
            )
            if response.status_code == 200:
                logger.info("✅ Batch Cancel Success", count=len(order_ids))
                return len(order_ids)
            return 0
        except Exception as e:
            logger.error("❌ Batch Cancel Failed", error=str(e))
            return 0

    async def close(self):
        """클라이언트 세션 종료"""
        await self.client.aclose()