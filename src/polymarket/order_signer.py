# src/polymarket/order_signer.py
from __future__ import annotations
from typing import Any
from eth_account import Account
from eth_account.messages import encode_typed_data
import structlog

logger = structlog.get_logger(__name__)

class OrderSigner:
    def __init__(self, private_key: str):
        self.account = Account.from_key(private_key)

    def get_address(self) -> str:
        """체크섬이 적용된 지갑 주소 반환"""
        return self.account.address

    def sign_clob_auth_message(self, timestamp: int, nonce: int = 0) -> str:
        """
        [공식 문서] API 키 생성을 위한 EIP-712 서명
        https://docs.polymarket.com/developers/CLOB/authentication#python-2
        """
        try:
            data = {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                    ],
                    "ClobAuth": [
                        {"name": "address", "type": "address"},
                        {"name": "timestamp", "type": "string"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "message", "type": "string"},
                    ]
                },
                "primaryType": "ClobAuth",
                "domain": {
                    "name": "ClobAuthDomain",
                    "version": "1",
                    "chainId": 137, # Polygon Mainnet
                },
                "message": {
                    "address": self.account.address,
                    "timestamp": str(timestamp),
                    "nonce": nonce,
                    "message": "This message attests that I control the given wallet"
                }
            }
            
            signable_message = encode_typed_data(full_message=data)
            signed_message = self.account.sign_message(signable_message)
            return signed_message.signature.hex()
            
        except Exception as e:
            logger.error("clob_auth_signing_failed", error=str(e))
            raise

    def sign_order(self, order_data: dict[str, Any]) -> str:
        try:
            data = {
                "types": {
                    "EIP712Domain": [
                        {"name": "name", "type": "string"},
                        {"name": "version", "type": "string"},
                        {"name": "chainId", "type": "uint256"},
                        {"name": "verifyingContract", "type": "address"},
                    ],
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
                },
                "primaryType": "Order",
                "domain": {
                    "name": "ClobMarket",
                    "version": "1",
                    "chainId": 137,
                    "verifyingContract": "0x4bFb9717357DeC1FEFc6033987243b9F701f9458"
                },
                "message": {
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
            }
            
            signable_message = encode_typed_data(full_message=data)
            signed_message = self.account.sign_message(signable_message)
            
            sig = signed_message.signature.hex()
            return sig if sig.startswith("0x") else "0x" + sig
        except Exception as e:
            logger.error("order_signing_failed", error=str(e))
            raise