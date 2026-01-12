# src/execution/order_executor.py
from __future__ import annotations
import asyncio
import httpx
from typing import Any, Dict, Optional, List
import structlog
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, AssetType, BalanceAllowanceParams
from src.config import Settings
from src.polymarket.order_signer import OrderSigner

logger = structlog.get_logger(__name__)

CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

SAFE_ABI = [
    {"inputs":[{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"value","type":"uint256"},{"internalType":"bytes","name":"data","type":"bytes"},{"internalType":"uint8","name":"operation","type":"uint8"},{"internalType":"uint256","name":"safeTxGas","type":"uint256"},{"internalType":"uint256","name":"baseGas","type":"uint256"},{"internalType":"uint256","name":"gasPrice","type":"uint256"},{"internalType":"address","name":"gasToken","type":"address"},{"internalType":"address","name":"refundReceiver","type":"address"},{"internalType":"bytes","name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"internalType":"bool","name":"success","type":"bool"}],"stateMutability":"payable","type":"function"}
]

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
        
        self.client = ClobClient(
            host=settings.polymarket_api_url,
            key=self.order_signer.get_private_key(),
            chain_id=137,
            signature_type=2,
            funder=self.safe_address
        )
        if settings.public_address:
            self.client.address = settings.public_address
            
        self.ctf_contract = self.w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
        self.safe_contract = self.w3.eth.contract(address=self.safe_address, abi=SAFE_ABI)
        self.usdc_contract = self.w3.eth.contract(address=Web3.to_checksum_address(USDC_ADDRESS), abi=ERC20_ABI)

    async def get_dynamic_gas_fees(self):
        """[참고: orderExecutor.ts] Polygon Gas Station V2를 사용한 동적 가스비 산출"""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get("https://gasstation.polygon.technology/v2", timeout=5.0)
                if response.status_code != 200:
                    raise Exception(f"Gas API Status {response.status_code}")
                
                data = response.json()
                fast = data['fast']
                
                # TS 로직 반영: Priority 1.3배, Max 1.5배 적용
                priority_fee = fast['maxPriorityFee'] * 1.3
                max_fee = fast['maxFee'] * 1.5
                
                return {
                    'maxPriorityFeePerGas': self.w3.to_wei(round(priority_fee, 9), 'gwei'),
                    'maxFeePerGas': self.w3.to_wei(round(max_fee, 9), 'gwei')
                }
        except Exception as e:
            # API 실패 시 백업: 현재 가스 시세의 2.5배 사용
            logger.warn(f"⚠️ 가스 API 실패, 백업 로직 가동: {str(e)}")
            base_fee = self.w3.eth.gas_price
            return {
                'maxPriorityFeePerGas': int(base_fee * 2.5),
                'maxFeePerGas': int(base_fee * 3.0) 
            }

    async def _execute_via_proxy(self, target_address: str, data: bytes) -> bool:
        """EOA 가스비 지불 + Proxy 자산 실행 통합 함수"""
        try:
            signer_addr = self.order_signer.get_address()
            signature = "0x000000000000000000000000" + signer_addr[2:].lower() + \
                        "0000000000000000000000000000000000000000000000000000000000000000" + "01"
            
            fees = await self.get_dynamic_gas_fees()
            
            txn_params = {
                'from': signer_addr,
                'nonce': self.w3.eth.get_transaction_count(signer_addr),
                'gas': 600000,
                'maxFeePerGas': fees['maxFeePerGas'],
                'maxPriorityFeePerGas': fees['maxPriorityFeePerGas'],
                'type': 2 # EIP-1559 트랜잭션
            }

            txn = self.safe_contract.functions.execTransaction(
                Web3.to_checksum_address(target_address),
                0, data, 0, 0, 0, 0,
                "0x0000000000000000000000000000000000000000",
                "0x0000000000000000000000000000000000000000",
                signature
            ).build_transaction(txn_params)

            signed_txn = self.w3.eth.account.sign_transaction(txn, self.order_signer.get_private_key())
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            
            logger.info("🚀 Proxy Transaction Sent", tx_hash=tx_hash.hex(), 
                        priority_gwei=fees['maxPriorityFeePerGas']/1e9)
            
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            if receipt.status == 1:
                logger.info("✅ Proxy Transaction Confirmed")
                return True
            return False
        except Exception as e:
            logger.error("❌ Proxy Execution Failed", error=str(e))
            return False

    async def split_assets(self, amount_usd: float, condition_id: str) -> bool:
        """Proxy를 통한 자산 분할 (Split)"""
        try:
            amount_raw = int(amount_usd * 1e6)
            allowance = self.usdc_contract.functions.allowance(self.safe_address, Web3.to_checksum_address(CTF_ADDRESS)).call()
            if allowance < amount_raw:
                logger.info("⏳ Proxy USDC Allowance 부족. 자동 Approve 중...")
                approve_data = self.usdc_contract.encode_abi("approve", [Web3.to_checksum_address(CTF_ADDRESS), 2**256 - 1])
                await self._execute_via_proxy(USDC_ADDRESS, approve_data)

            parent_id = "0x" + "0" * 64
            partition = [1, 2]
            call_data = self.ctf_contract.encode_abi("splitPosition", [
                Web3.to_checksum_address(USDC_ADDRESS), parent_id, condition_id, partition, amount_raw
            ])
            return await self._execute_via_proxy(CTF_ADDRESS, call_data)
        except Exception as e:
            logger.error("❌ Split Failed", error=str(e))
            return False

    async def merge_assets(self, amount_shares: float, condition_id: str) -> bool:
        """Proxy를 통한 자산 병합 (Merge)"""
        try:
            amount_raw = int(amount_shares * 1e6)
            parent_id = "0x" + "0" * 64
            partition = [1, 2]
            call_data = self.ctf_contract.encode_abi("mergePositions", [
                Web3.to_checksum_address(USDC_ADDRESS), parent_id, condition_id, partition, amount_raw
            ])
            return await self._execute_via_proxy(CTF_ADDRESS, call_data)
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