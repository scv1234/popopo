from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from src.config import Settings
from src.inventory.inventory_manager import InventoryManager

logger = structlog.get_logger(__name__)


@dataclass
class Quote:
    side: str
    price: float
    size: float
    market: str
    token_id: str


class QuoteEngine:
    def __init__(self, settings: Settings, inventory_manager: InventoryManager):
        self.settings = settings
        self.inventory_manager = inventory_manager

    def calculate_bid_price(self, mid_price: float, spread_bps: int) -> float:
        return mid_price * (1 - spread_bps / 10000)

    def calculate_ask_price(self, mid_price: float, spread_bps: int) -> float:
        return mid_price * (1 + spread_bps / 10000)

    def calculate_mid_price(self, best_bid: float, best_ask: float) -> float:
        if best_bid <= 0 or best_ask <= 0:
            return 0.0
        return (best_bid + best_ask) / 2.0

    def generate_quotes(
        self, 
        market_id: str, 
        best_bid: float, 
        best_ask: float, 
        yes_token_id: str, 
        no_token_id: str, 
        spread_cents: float,
        min_size_shares: float,
        user_input_shares: float = None,  # <--- 이 매개변수를 추가하세요
    ) -> tuple[Quote | None, Quote | None]:

        # 수량이 입력되지 않았다면 기본값(Settings.default_size) 사용
        size = user_input_shares if user_input_shares is not None else self.settings.default_size
        """
        보상 범위의 끝단(90%)에 주문을 배치하여 체결 확률은 낮추고 리워드 자격은 유지합니다.
        """
        # 1. 중간가(Mid-price) 계산
        mid_price = self.calculate_mid_price(best_bid, best_ask)
        
        if mid_price == 0:
            return (None, None)

        # 2. 안전 마진 계산 (보상 범위의 90% 지점)
        # 예: max_spread가 3c(0.03)라면, 중간가에서 2.7c(0.027) 떨어진 곳에 주문 배치
        margin_usd = spread_cents * 0.9 / 100
        
        # 3. YES 및 NO 토큰의 매수 가격 결정
        # YES 매수가는 중간가보다 낮게, NO 매수가는 (YES 매도가의 반대이므로) 중간가보다 높게 설정
        bid_price = round(mid_price - margin_usd, 3)
        ask_price = round(mid_price + margin_usd, 3) # YES를 이 가격에 팔겠다는 의미
        
        # NO 토큰의 매수 가격은 (1 - YES 매도가)와 같음
        no_bid_price = round(1.0 - ask_price, 3)
        
        # 2. 수량 결정 로직 (사용자 입력 우선)
        # 사용자가 입력한 수량을 사용하되, 실수 방지를 위해 min_size보다는 크게 잡습니다.
        final_shares = max(size, min_size_shares)
        
        if user_input_shares < min_size_shares:
            logger.warning("user_input_below_min_shares", 
                           input=user_input_shares, 
                           min_required=min_size_shares)

        # 3. 인벤토리 매니저 확인 (수량 기반 델타 뉴트럴 유지)
        # 사용자가 정한 수량을 기준으로 하되, 한쪽이 이미 너무 많다면 
        # 인벤토리 매니저가 수량을 조절(절반으로 줄임 등)하게 할 수 있습니다.
        yes_shares = self.inventory_manager.get_quote_size_yes(final_shares)
        no_shares = self.inventory_manager.get_quote_size_no(final_shares)

        # 4. 최종 Quote 객체 생성
        yes_quote = None
        if self.inventory_manager.can_quote_yes(yes_shares):
            yes_quote = Quote(
                side="BUY",
                price=bid_price,
                size=yes_shares,
                market=market_id,
                token_id=yes_token_id,
            )

        no_quote = None
        if self.inventory_manager.can_quote_no(no_shares):
            no_quote = Quote(
                side="BUY",
                price=no_bid_price,
                size=no_shares,
                market=market_id,
                token_id=no_token_id,
            )

        return (yes_quote, no_quote)

    def adjust_for_inventory_skew(self, base_size: float, price: float, side: str) -> float:
        skew = self.inventory_manager.inventory.get_skew()
        
        if skew > 0.2:
            if side == "BUY" and self.inventory_manager.inventory.net_exposure_usd > 0:
                return base_size * 0.5
            elif side == "SELL" and self.inventory_manager.inventory.net_exposure_usd < 0:
                return base_size * 0.5
        
        return base_size

    def should_trim_quotes(self, time_to_close_hours: float) -> bool:
        if time_to_close_hours < 1.0:
            return True
        return False

