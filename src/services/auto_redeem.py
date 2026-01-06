from __future__ import annotations

from typing import Any

import httpx
import structlog

from src.config import Settings

logger = structlog.get_logger(__name__)

class AutoRedeem:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=30.0)

    async def check_redeemable_positions(self, address: str) -> list[dict[str, Any]]:
        try:
            # [수정] CLOB API 대신 Gamma API 사용
            data_api_url = "https://data-api.polymarket.com/positions" # 도메인을 data-api로 변경
            
            response = await self.client.get(
                data_api_url,
                params={"user": address, "redeemable": "true"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("redeemable_positions_check_failed", error=str(e))
            return []

    async def redeem_position(self, position_id: str) -> bool:
        try:
            response = await self.client.post(
                f"{self.settings.polymarket_api_url}/redeem/{position_id}",
            )
            response.raise_for_status()
            logger.info("position_redeemed_success", position_id=position_id)
            return True
        except Exception as e:
            logger.error("position_redeem_failed", position_id=position_id, error=str(e))
            return False

    async def auto_redeem_all(self, address: str) -> int:
        """종료된 마켓의 토큰을 USDC로 자동 전환하여 보상을 확정합니다."""
        if not self.settings.auto_redeem_enabled:
            return 0
        
        redeemable = await self.check_redeemable_positions(address)
        redeemed = 0
        
        for position in redeemable:
            # 설정된 최소 금액(USD) 이상일 때만 리딤 실행
            value_usd = float(position.get("value", 0))
            if value_usd >= self.settings.redeem_threshold_usd:
                if await self.redeem_position(position.get("id")):
                    redeemed += 1
        
        if redeemed > 0:
            logger.info("auto_redeem_summary", redeemed_count=redeemed, total_found=len(redeemable))
        return redeemed

    async def close(self):
        await self.client.aclose()