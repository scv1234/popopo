# src/polymarket/order_signer.py
from __future__ import annotations
from typing import Any
from eth_account import Account
from eth_account.messages import encode_typed_data
import structlog
import json

logger = structlog.get_logger(__name__)

class OrderSigner:
    def __init__(self, private_key: str):
        self.account = Account.from_key(private_key)

    def get_address(self) -> str:
        # [핵심] 원래의 체크섬 주소 반환 (대소문자 섞임)
        return self.account.address

    def sign_clob_auth_message(self, timestamp: int, nonce: int = 0) -> str:
        """[수정] API 키 발급용 EIP-712 서명 (Checksum Address 사용)"""
        try:
            # .lower() 제거 -> 원래 주소 사용
            checksum_address = self.account.address
            
            logger.info(f"DEBUG_SIGNER: Signing Address: {checksum_address}")
            logger.info(f"DEBUG_SIGNER: Timestamp: {timestamp}, Nonce: {nonce}")

            # EIP-712 데이터 구성
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
                    "chainId": 137,
                },
                "message": {
                    "address": checksum_address, # [중요] 체크섬 주소 사용
                    "timestamp": str(timestamp),
                    "nonce": int(nonce),
                    "message": "This message attests that I control the given wallet"
                }
            }
            
            # 디버깅용 출력
            # print(f"\n[DEBUG_SIGNER] Full Data Structure:\n{json.dumps(data, default=str, indent=2)}\n")
            
            signable_message = encode_typed_data(full_message=data)
            signed_message = self.account.sign_message(signable_message)
            
            # 0x 접두어 처리
            sig = signed_message.signature.hex()
            if not sig.startswith("0x"):
                sig = "0x" + sig
            
            return sig
            
        except Exception as e:
            logger.error("clob_auth_signing_failed", error=str(e))
            raise

    def sign_order(self, order_data: dict[str, Any]) -> str:
        """주문용 EIP-712 서명 (기존 로직 유지 + 0x 접두어 확인)"""
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
            if not sig.startswith("0x"):
                sig = "0x" + sig
            return sig
            
        except Exception as e:
            logger.error("order_signing_failed", error=str(e))
            raise