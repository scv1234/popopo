# src/polymarket/order_signer.py
from __future__ import annotations
from typing import Any
from eth_account import Account
from eth_account.messages import encode_typed_data, encode_defunct # [추가]
import structlog

logger = structlog.get_logger(__name__)

class OrderSigner:
    def __init__(self, private_key: str):
        self.account = Account.from_key(private_key)

    def get_address(self) -> str:
        return self.account.address

    def sign_text(self, text: str) -> str:
        """[추가] API 키 발급 시 필요한 일반 텍스트 서명"""
        message = encode_defunct(text=text)
        signed_message = self.account.sign_message(message)
        return signed_message.signature.hex()

    def sign_order(self, order_data: dict[str, Any]) -> str:
        """폴리마켓 CLOB EIP-712 서명 (기존 로직 동일)"""
        try:
            domain = {
                "name": "ClobMarket",
                "version": "1",
                "chainId": 137,
                "verifyingContract": "0x4bFb9717357DeC1FEFc6033987243b9F701f9458"
            }
            types = {
                "Order": [
                    {"name": "maker", "type": "address"},
                    {"name": "taker", "type": "address"},
                    {"name": "tokenId", "type": "uint256"},
                    {"name": "makerAmount", "type": "uint256"},
                    {"name": "takerAmount", "type": "uint256"},
                    {"name": "side", "type": "uint256"},
                    {"name": "feeRateBps", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "signer", "type": "address"},
                    {"name": "expiration", "type": "uint256"},
                    {"name": "salt", "type": "uint256"},
                    {"name": "signatureType", "type": "uint256"},
                ]
            }
            message = {
                "maker": order_data["maker"],
                "taker": order_data["taker"],
                "tokenId": int(order_data["tokenId"]),
                "makerAmount": int(order_data["makerAmount"]),
                "takerAmount": int(order_data["takerAmount"]),
                "side": int(order_data["side"]),
                "feeRateBps": int(order_data["feeRateBps"]),
                "nonce": int(order_data["nonce"]),
                "signer": order_data["signer"],
                "expiration": int(order_data["expiration"]),
                "salt": int(order_data["salt"]),
                "signatureType": int(order_data["signatureType"])
            }
            structured_data = encode_typed_data(
                domain_data=domain, 
                types=types, 
                primary_type="Order", 
                message=message
            )
            signed_message = self.account.sign_message(structured_data)
            return signed_message.signature.hex()
        except Exception as e:
            logger.error("order_signing_failed", error=str(e), order_data=order_data)

            raise
