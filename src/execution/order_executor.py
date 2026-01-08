# src/execution/order_executor.py
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, List
import structlog

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, AssetType, BalanceAllowanceParams
from src.config import Settings
from src.polymarket.order_signer import OrderSigner

logger = structlog.get_logger(__name__)

class OrderExecutor:
    def __init__(self, settings: Settings, order_signer: OrderSigner):
        self.settings = settings
        self.order_signer = order_signer
        
        # 1. ClobClient 초기화 (address 인자 제거)
        self.client = ClobClient(
            host=settings.polymarket_api_url,
            key=self.order_signer.get_private_key(),
            chain_id=137,
            signature_type=2, 
            funder=self.order_signer.get_address()
        )
        
        # 2. Proxy(Safe) 지갑 주소를 객체 속성에 직접 설정
        # 이렇게 해야 SDK가 주문의 maker 주소를 Safe 주소로 올바르게 인식합니다.
        if settings.public_address:
            self.client.address = settings.public_address
            
        self.safe_address = settings.public_address

    async def initialize(self):
        """API 자격 증명 유도 및 설정 (L2 인증)"""
        try:
            logger.info("initializing_clob_auth")
            
            # API Creds 유도 (기존의 수동 HMAC 생성을 대체)
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            
            logger.info("✅ CLOB Auth Initialized", 
                        address=self.client.get_address(),
                        mode=self.client.mode)
        except Exception as e:
            logger.error("❌ CLOB Auth Failed", error=str(e))
            raise

    async def place_order(self, order_params: Dict[str, Any]) -> Optional[Dict]:
        """주문 생성 (main.py의 'token_id'와 'id' 기대치 충족)"""
        try:
            # 변수명 통일: main.py에서 보내는 token_id를 사용
            order_args = OrderArgs(
                token_id=order_params["token_id"], 
                price=float(order_params["price"]),
                size=float(order_params["size"]),
                side=order_params["side"].upper()
            )

            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)
            
            # main.py 호환성: 'orderID'를 'id'로 복사하여 반환
            if result and "orderID" in result:
                result["id"] = result["orderID"]
            
            return result
        except Exception as e:
            logger.error("❌ Order Placement Failed", error=str(e))
            return None

    async def place_market_order(self, market_id: str, side: str, size: float, token_id: str) -> Optional[Dict]:
        """긴급 청산용 주문 (유리한 가격으로 지정가 주문 제출)"""
        price = 0.99 if side == "BUY" else 0.01
        return await self.place_order({
            "side": side,
            "size": size,
            "price": price,
            "token_id": token_id
        })

    async def get_usdc_balance(self) -> float:
        """현재 계정의 USDC 잔고 조회"""
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            balance_data = self.client.get_balance_allowance(params=params)
            
            # 6자리 소수점(USDC) 적용하여 변환
            return float(balance_data.get("balance", 0)) / 1e6
        except Exception as e:
            logger.error("❌ Failed to fetch balance", error=str(e))
            return 0.0

    async def cancel_order(self, order_id: str) -> bool:
        """개별 주문 취소"""
        try:
            self.client.cancel(order_id)
            logger.info("✅ Order Cancelled", order_id=order_id)
            return True
        except Exception as e:
            logger.error("❌ Cancel Failed", order_id=order_id, error=str(e))
            return False

    async def batch_cancel_orders(self, order_ids: List[str]) -> int:
        """여러 주문 ID 일괄 취소"""
        if not order_ids:
            return 0
        try:
            self.client.cancel_orders(order_ids)
            logger.info("✅ Batch Cancel Success", count=len(order_ids))
            return len(order_ids)
        except Exception as e:
            logger.error("❌ Batch Cancel Failed", error=str(e))
            return 0

    async def cancel_all_orders(self, market_id: str = None) -> bool:
        """모든 주문 취소 (인자 허용하여 main.py와 호환성 유지)"""
        try:
            self.client.cancel_all()
            logger.info("✅ All Orders Cancelled")
            return True
        except Exception as e:
            logger.error("❌ Cancel All Failed", error=str(e))
            return False

    async def close(self):
        """세션 종료 (SDK는 동기 방식이므로 pass 처리)"""
        pass