# src/polymarket/order_signer.py
from __future__ import annotations
from eth_account import Account
import structlog

logger = structlog.get_logger(__name__)

class OrderSigner:
    def __init__(self, private_key: str):
        # 0x 접두어 확인 및 계정 로드
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self.account = Account.from_key(private_key)
        self.private_key = private_key

    def get_address(self) -> str:
        """EOA 지갑 주소 반환"""
        return self.account.address

    def get_private_key(self) -> str:
        """SDK에 전달할 개인키 반환"""
        return self.private_key