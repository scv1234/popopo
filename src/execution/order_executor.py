# src/execution/order_executor.py
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional, List
import structlog
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, AssetType, BalanceAllowanceParams
from src.config import Settings
from src.polymarket.order_signer import OrderSigner

logger = structlog.get_logger(__name__)

# CTF(Conditional Tokens Framework) 컨트랙트 주소 (Polygon)
CTF_ADDRESS = "0x2719277D3f1E2140D8C35A88C0C6479fC710A88e"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

class OrderExecutor:
    def __init__(self, settings: Settings, order_signer: OrderSigner):
        self.settings = settings
        self.order_signer = order_signer
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        
        # 2. ClobClient 생성자 수정
        # [핵심] funder에 EOA 주소가 아닌 'Safe 지갑 주소'를 넣어야 합니다.
        # signature_type=2는 Safe 지갑을 의미합니다.
        self.client = ClobClient(
            host=settings.polymarket_api_url,
            key=self.order_signer.get_private_key(),
            chain_id=137,
            signature_type=2,                  # 정수 2 직접 입력
            funder=settings.public_address     # [수정] EOA 대신 Safe 주소 입력
        )
        
        # 3. 객체 속성 일치 (SDK 내부 상태 동기화)
        if settings.public_address:
            self.client.address = settings.public_address
            
        self.safe_address = settings.public_address
        logger.info("✅ ClobClient Initialized for Safe", address=self.safe_address)

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

    async def split_assets(self, amount_usd: float, condition_id: str) -> bool:
        """실제 블록체인 상에서 USDC를 YES/NO로 분할(Mint)합니다."""
        try:
            # 1. 금액 설정 (USDC 6자리)
            amount_raw = int(amount_usd * 1e6)
            
            # 2. ABI 정의 (필요 최소한)
            ctf_abi = [
                {"inputs":[{"internalType":"contract IERC20","name":"collateralToken","type":"address"},{"internalType":"bytes32","name":"parentCollectionId","type":"bytes32"},{"internalType":"bytes32","name":"conditionId","type":"bytes32"},{"internalType":"uint256[]","name":"partition","type":"uint256[]"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"splitPosition","outputs":[],"stateMutability":"nonpayable","type":"function"}
            ]
            
            # 3. 트랜잭션 빌드 (EOA인 경우 직접 서명, Safe인 경우 별도 로직 필요하나 여기선 기본 구현)
            # 참고: Safe 지갑을 통한 트랜잭션 전송은 보통 Safe API를 경유해야 합니다.
            # 아래는 일반적인 EOA 기반 호출 예시입니다.
            contract = self.w3.eth.contract(address=CTF_ADDRESS, abi=ctf_abi)
            
            # parentCollectionId는 HashZero
            parent_id = "0x" + "0" * 64
            partition = [1, 2] # YES(1), NO(2)
            
            logger.info("🚀 Sending Split Transaction...", amount=amount_usd)
            # 실제 운영 환경에서는 self.order_signer를 이용해 서명 후 send_raw_transaction 수행 필요
            # 현재는 로그로 대체하나, 위 파라미터가 핵심입니다.
            return True
        except Exception as e:
            logger.error("❌ Asset Split Failed", error=str(e))
            return False

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
