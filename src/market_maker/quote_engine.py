from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import math
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
        volatility: float = 0.01,         # [고도화] 변동성 인자 추가
        user_input_shares: float = None, 
    ) -> tuple[Quote | None, Quote | None]:
        """
        [고도화 버전] 
        1. 변동성에 따른 동적 스프레드 적용
        2. 인벤토리 불균형에 따른 가격 스큐(Skewing) 적용
        3. 보상 범위(90% 지점) 최적화 유지
        """
        
        # 0. 기본 주문 수량 결정
        size = user_input_shares if user_input_shares is not None else self.settings.default_size
        final_shares = max(size, min_size_shares)

        # 1. 중간가(Mid-price) 계산
        mid_price = self.calculate_mid_price(best_bid, best_ask)
        if mid_price == 0:
            return (None, None)

        # 2. [고도화] 동적 스프레드 (Dynamic Spread)
        # 변동성이 높을수록 안전 마진을 위해 스프레드를 확대합니다. (1.0x ~ 2.5x)
        volatility_multiplier = max(1.0, min(2.5, 1 + (volatility * 15)))
        dynamic_spread_usd = (spread_cents * volatility_multiplier) / 100.0

        # 3. [고도화] 가격 스큐 (Price Skewing)
        # 내 지갑의 YES/NO 수량 차이를 확인하여 가격을 비틉니다.
        # net_exposure_shares가 양수(+)면 YES가 많음 -> 주문 가격을 낮춤 (SELL 유도)
        inventory_diff = self.inventory_manager.inventory.net_exposure_shares
        
        # 스큐 강도: 1000주 차이당 0.005달러(0.5센트) 가격 이동
        skew_adjustment = (inventory_diff / 1000) * 0.005
        
        # 보상 범위를 최대한 활용하기 위한 마진 (동적 스프레드의 90% 지점)
        margin_usd = dynamic_spread_usd * 0.9
        
        # [핵심] 스큐가 적용된 중간가(Skewed Mid)를 기준으로 최종 가격 산출
        skewed_mid = mid_price - skew_adjustment
        
        # YES 매수가 (중간가보다 낮게)
        bid_price = round(skewed_mid - margin_usd, 3)
        # YES 매도가 (중간가보다 높게)
        ask_price = round(skewed_mid + margin_usd, 3)
        
        # NO 토큰의 매수 가격은 (1 - YES 매도가)
        no_bid_price = round(1.0 - ask_price, 3)

        # 4. 인벤토리 상태에 따른 수량 조절 (기존 델타 뉴트럴 유지)
        # 수량 조절과 가격 조절(Skew)이 동시에 작동하여 시너지를 냅니다.
        yes_shares = self.inventory_manager.get_quote_size_yes(final_shares)
        no_shares = self.inventory_manager.get_quote_size_no(final_shares)

        # 5. 최종 Quote 생성 및 안전 범위 검사
        yes_quote = None
        if self.inventory_manager.can_quote_yes(yes_shares) and 0.01 < bid_price < 0.99:
            yes_quote = Quote(
                side="BUY",
                price=bid_price,
                size=yes_shares,
                market=market_id,
                token_id=yes_token_id,
            )

        no_quote = None
        if self.inventory_manager.can_quote_no(no_shares) and 0.01 < no_bid_price < 0.99:
            no_quote = Quote(
                side="BUY",
                price=no_bid_price,
                size=no_shares,
                market=market_id,
                token_id=no_token_id,
            )

        # 로그 기록 (디버깅용)
        if yes_quote or no_quote:
            logger.debug("quotes_generated", 
                         skew=round(skew_adjustment, 4), 
                         vol_mult=round(volatility_multiplier, 2),
                         yes_p=bid_price, 
                         no_p=no_bid_price)

        return (yes_quote, no_quote)

    def should_trim_quotes(self, time_to_close_hours: float) -> bool:
        """마감 임박 시 리스크 방지를 위해 주문을 중단합니다."""
        # 설정파일의 avoid_near_expiry_hours와 연동 가능
        return time_to_close_hours < 1.0


