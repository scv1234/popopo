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

    def round_to_tick(self, price: float, tick_size: float) -> float:
        """시장의 최소 단위(tick_size)에 맞춰 가격을 반올림합니다."""
        if tick_size <= 0: return round(price, 2)
        # 예: tick_size가 0.01이면 소수점 2자리, 0.001이면 3자리로 계산
        precision = int(-math.log10(tick_size))
        return round(math.floor(price / tick_size) * tick_size, precision)    

    def generate_quotes(
        self, 
        market_id: str, 
        best_bid: float, 
        best_ask: float, 
        yes_token_id: str, 
        no_token_id: str, 
        spread_cents: float,
        min_size_shares: float,
        tick_size: float = 0.01, # 기본값 설정
        volatility_1h: float = 0.005,         # [고도화] 변동성 인자 추가
        user_input_shares: float = None, 
    ) -> tuple[Quote | None, Quote | None]:
        """
        [전략 수정 버전] 
        1. 4.5% 미만: 보상 최적화 모드 (1.0배 고정 스프레드)
        2. 4.5% 이상: 동적 방어 모드 (변동성 배율 적용, 최대 3.0배)
        3. 모든 구간에서 주문 마진은 스프레드의 90% 유지
        """
        
        # 1. 기본 주문 수량 결정
        size = user_input_shares if user_input_shares is not None else self.settings.default_size
        final_shares = max(size, min_size_shares)

        # 2. 중간가(Mid-price) 계산
        mid_price = self.calculate_mid_price(best_bid, best_ask)
        if mid_price == 0:
            return (None, None)

        # 3. [핵심 로직] 변동성에 따른 배율 결정
        if volatility_1h < 0.045:
            # 안정적일 때는 1.0배 고정하여 보상 획득에 집중
            volatility_multiplier = 1.0
        else:
            # 0.045 이상일 때는 동적 스프레드를 활성화하여 위험 회피 (최대 3.0배)
            volatility_multiplier = max(1.0, min(3.0, 1 + (volatility_1h * 100)))
            logger.warning("🚨 HIGH_VOLATILITY_DYNAMIC_DEFENSE", 
                           vol=round(volatility_1h, 4), 
                           multiplier=volatility_multiplier)

        # 최종 스프레드 계산
        dynamic_spread_usd = (spread_cents * volatility_multiplier) / 100.0

        # 4. 가격 스큐 (Price Skewing) 유지
        inventory_diff = self.inventory_manager.inventory.net_exposure_shares
        skew_adjustment = (inventory_diff / 1000) * 0.005
        
        # 보상 범위 또는 방어 범위를 활용하기 위한 90% 마진 적용
        margin_usd = dynamic_spread_usd * 0.9
        
        # 스큐 적용 중간가 산출
        skewed_mid = mid_price - skew_adjustment
        
        # YES/NO 주문 가격 산출
        bid_price = self.round_to_tick(skewed_mid - margin_usd, tick_size)
        ask_price = self.round_to_tick(skewed_mid + margin_usd, tick_size)
        no_bid_price = self.round_to_tick(1.0 - ask_price, tick_size)

        # 5. 최종 Quote 생성
        yes_shares = self.inventory_manager.get_quote_size_yes(final_shares)
        no_shares = self.inventory_manager.get_quote_size_no(final_shares)

        yes_quote = None
        if self.inventory_manager.can_quote_yes(yes_shares) and 0.01 < bid_price < 0.99:
            yes_quote = Quote(
                side="BUY", price=bid_price, size=yes_shares,
                market=market_id, token_id=yes_token_id
            )

        no_quote = None
        if self.inventory_manager.can_quote_no(no_shares) and 0.01 < no_bid_price < 0.99:
            no_quote = Quote(
                side="BUY", price=no_bid_price, size=no_shares,
                market=market_id, token_id=no_token_id
            )

        return (yes_quote, no_quote)

    def should_trim_quotes(self, time_to_close_hours: float) -> bool:
        """마감 임박 시 리스크 방지를 위해 주문을 중단합니다."""
        # 설정파일의 avoid_near_expiry_hours와 연동 가능
        return time_to_close_hours < 1.0


