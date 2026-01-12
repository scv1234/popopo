# src/execution/order_executor.py
from __future__ import annotations
import asyncio
import httpx
from typing import Any, Dict, Optional, List
import structlog
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType
# 가스리스 실행을 위한 라이브러리 추가
from py_builder_relayer_client.client import RelayClient
# SafeTransaction 클래스 임포트 추가
from py_builder_signing_sdk.config import BuilderConfig, BuilderApiKeyCreds
from py_builder_relayer_client.models import SafeTransaction, OperationType
from src.config import Settings
from src.polymarket.order_signer import OrderSigner

logger = structlog.get_logger(__name__)

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# Safe ABI는 릴레이어 클라이언트가 내부적으로 처리하므로 명시적 호출용 외에는 필요성이 줄어듭니다.
CTF_ABI = [
    {"inputs":[{"internalType":"contract IERC20","name":"collateralToken","type":"address"},{"internalType":"bytes32","name":"parentCollectionId","type":"bytes32"},{"internalType":"bytes32","name":"conditionId","type":"bytes32"},{"internalType":"uint256[]","name":"partition","type":"uint256[]"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"splitPosition","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"contract IERC20","name":"collateralToken","type":"address"},{"internalType":"bytes32","name":"parentCollectionId","type":"bytes32"},{"internalType":"bytes32","name":"conditionId","type":"bytes32"},{"internalType":"uint256[]","name":"partition","type":"uint256[]"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"mergePositions","outputs":[],"stateMutability":"nonpayable","type":"function"}
]

ERC20_ABI = [
    {"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"},
    {"constant":True,"inputs":[{"name":"_owner","type":"address"},{"name":"_spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"constant":False,"inputs":[{"name":"_spender","type":"address"},{"name":"_value","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}
]

class OrderExecutor:
    def __init__(self, settings: Settings, order_signer: OrderSigner):
        self.settings = settings
        self.order_signer = order_signer
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        self.safe_address = Web3.to_checksum_address(settings.public_address)
        
        # 1. CLOB Client 초기화
        self.client = ClobClient(
            host=settings.polymarket_api_url,
            key=self.order_signer.get_private_key(),
            chain_id=137,
            signature_type=2,
            funder=self.safe_address
        )
        if settings.public_address:
            self.client.address = settings.public_address

        # 2. Gasless 실행을 위한 RelayClient 설정
        builder_creds = BuilderApiKeyCreds(
            key=settings.polymarket_builder_api_key,
            secret=settings.polymarket_builder_secret,
            passphrase=settings.polymarket_builder_passphrase
        )
        builder_config = BuilderConfig(local_builder_creds=builder_creds)
        
        # [수정] 확인된 시그니처에 맞춰 tx_type 제거
        self.relay_client = RelayClient(
            relayer_url="https://relayer-v2.polymarket.com/", 
            chain_id=137,
            private_key=self.order_signer.get_private_key(), 
            builder_config=builder_config
        )
            
        self.ctf_contract = self.w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
        self.usdc_contract = self.w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)

    async def _execute_gasless(self, transactions: List[Dict[str, Any]], label: str = "Task") -> bool:
        """폴리마켓 릴레이어를 통한 가스리스 실행 핵심 함수"""
        try:
            # SafeTransaction 객체 리스트 생성 (기존 로직 유지)
            safe_txs = [
                SafeTransaction(
                    to=Web3.to_checksum_address(tx["to"]),
                    operation=OperationType.Call,
                    data=tx["data"],
                    value=str(tx.get("value", "0"))
                ) for tx in transactions
            ]

            # 릴레이어 실행 요청
            response = self.relay_client.execute(
                transactions=safe_txs, 
                metadata=label
            )
            
            # [핵심 수정] SDK 객체 속성 이름(transaction_id, transaction_hash)에 맞춰 추출
            tx_id = getattr(response, "transaction_id", None)
            tx_hash = getattr(response, "transaction_hash", None) or getattr(response, "hash", None)
            
            if tx_id:
                logger.info(f"🚀 Gasless {label} Submitted", tx_id=tx_id, tx_hash=tx_hash)
                
                # [개선] SDK 자체의 .wait() 기능을 사용하여 트랜잭션이 확정될 때까지 대기합니다.
                # 이 함수는 릴레이어의 내부 상태를 폴링하므로 더 정확합니다.
                # (주의: wait()는 동기 함수이므로 asyncio.to_thread를 사용하여 루프 차단을 방지합니다)
                result = await asyncio.to_thread(response.wait)
                
                if result:
                    # 결과 데이터에서 실제 블록에 기록된 해시를 가져와 로그를 찍습니다.
                    final_hash = result.get("transactionHash") or tx_hash
                    logger.info(f"✅ Gasless {label} Confirmed", tx_hash=final_hash)
                    return True
                else:
                    logger.error(f"❌ Gasless {label} Failed in Relayer", tx_id=tx_id)
                    return False
            
            logger.error(f"❌ Gasless {label} Submission Failed (No ID)", response=response)
            return False
            
        except Exception as e:
            logger.error(f"❌ Gasless Execution Error", label=label, error=str(e))
            return False

    async def split_assets(self, amount_usd: float, condition_id: str) -> bool:
        """가스리스 자산 분할 (Split)"""
        try:
            amount_raw = int(amount_usd * 1e6)
            txs = []

            # 1. Allowance 체크 및 필요 시 Approve 추가
            allowance = self.usdc_contract.functions.allowance(self.safe_address, Web3.to_checksum_address(CTF_ADDRESS)).call()
            if allowance < amount_raw:
                approve_data = self.usdc_contract.encode_abi("approve", [Web3.to_checksum_address(CTF_ADDRESS), 2**256 - 1])
                txs.append({
                    "to": USDC_ADDRESS,
                    "data": approve_data,
                    "value": "0"
                })

            # 2. Split Call Data 생성
            parent_id = "0x" + "0" * 64
            partition = [1, 2]
            split_data = self.ctf_contract.encode_abi("splitPosition", [
                Web3.to_checksum_address(USDC_ADDRESS), parent_id, condition_id, partition, amount_raw
            ])
            txs.append({
                "to": CTF_ADDRESS,
                "data": split_data,
                "value": "0"
            })

            # 릴레이어를 통해 일괄 실행 (Approve + Split)
            return await self._execute_gasless(txs, "SplitPosition")
        except Exception as e:
            logger.error("❌ Split Failed", error=str(e))
            return False

    async def merge_assets(self, amount_shares: float, condition_id: str) -> bool:
        """가스리스 자산 병합 (Merge)"""
        try:
            amount_raw = int(amount_shares * 1e6)
            parent_id = "0x" + "0" * 64
            partition = [1, 2]
            
            merge_data = self.ctf_contract.encode_abi("mergePositions", [
                Web3.to_checksum_address(USDC_ADDRESS), parent_id, condition_id, partition, amount_raw
            ])
            
            transaction = {
                "to": CTF_ADDRESS,
                "data": merge_data,
                "value": "0"
            }
            
            return await self._execute_gasless([transaction], "MergePositions")
        except Exception as e:
            logger.error("❌ Merge Failed", error=str(e))
            return False

    async def initialize(self):
        try:
            api_creds = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(api_creds)
            logger.info("✅ CLOB Auth Initialized")
        except Exception as e:
            logger.error("❌ CLOB Auth Failed", error=str(e))
            raise

    async def place_order(self, order_params: Dict[str, Any]) -> Optional[Dict]:
        try:
            order_args = OrderArgs(
                token_id=order_params["token_id"], 
                price=float(order_params["price"]),
                size=float(order_params["size"]),
                side=order_params["side"].upper()
            )
            signed_order = self.client.create_order(order_args)
            result = self.client.post_order(signed_order, OrderType.GTC)
            if result and "orderID" in result:
                result["id"] = result["orderID"]
            return result
        except Exception as e:
            logger.error("❌ Order Placement Failed", error=str(e))
            return None

    async def get_usdc_balance(self) -> float:
        try:
            balance = self.usdc_contract.functions.balanceOf(self.safe_address).call()
            return float(balance) / 1e6
        except Exception as e:
            logger.error("❌ Failed to fetch balance", error=str(e))
            return 0.0

    async def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel_order({"orderID": order_id})
            return True
        except Exception as e:
            logger.error("❌ Cancel Failed", order_id=order_id, error=str(e))
            return False

    async def batch_cancel_orders(self, order_ids: List[str]) -> int:
        if not order_ids: return 0
        try:
            # py_clob_client 사양에 맞춤
            for oid in order_ids:
                self.client.cancel_order({"orderID": oid})
            return len(order_ids)
        except Exception as e:
            logger.error("❌ Batch Cancel Failed", error=str(e))
            return 0

    async def cancel_all_orders(self, market_id: str = None) -> bool:
        try:
            self.client.cancel_all()
            return True
        except Exception as e:
            logger.error("❌ Cancel All Failed", error=str(e))
            return False

    async def close(self):
        pass