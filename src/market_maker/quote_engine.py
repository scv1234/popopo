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

    def ceil_to_tick(self, price: float, tick_size: float) -> float:
        """
        보상 범위 내에서 가장 먼 가격(최외곽 틱)을 찾기 위해 올림 처리를 합니다.
        부동소수점 오차를 방지하기 위해 정규화 과정을 거칩니다.
        """
        if tick_size <= 0: return round(price, 2)
        precision = int(-math.log10(tick_size))
        
        # 8자리 반올림으로 미세 오차 제거 후 틱 단위 올림(ceil)
        normalized_price = round(price / tick_size, 8)
        return round(math.ceil(normalized_price) * tick_size, precision)   

    def generate_quotes(
        self, 
        market_id: str, 
        yes_best_bid: float, yes_best_ask: float,
        no_best_bid: float, no_best_ask: float,
        yes_token_id: str, no_token_id: str, 
        spread_cents: float,
        min_size_shares: float,
        tick_size: float = 0.01,
        yes_vol_1h: float = 0.005,  # YES 변동성 분리
        no_vol_1h: float = 0.005,   # NO 변동성 분리
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
        yes_mid = self.calculate_mid_price(yes_best_bid, yes_best_ask)
        no_mid = self.calculate_mid_price(no_best_bid, no_best_ask)
    
        # 두 토큰 모두 유효한 호가가 없으면 주문을 생성하지 않음
        if yes_mid == 0 and no_mid == 0:
            return (None, None)

        # 3. YES 변동성 배율 및 마진 계산
        yes_mult = 1.0
        if yes_vol_1h >= 0.045:
            yes_mult = max(1.0, min(3.0, 1 + (yes_vol_1h * 100)))
        yes_margin = (spread_cents * yes_mult) / 100.0

        # 4. NO 변동성 배율 및 마진 계산
        no_mult = 1.0
        if no_vol_1h >= 0.045:
            no_mult = max(1.0, min(3.0, 1 + (no_vol_1h * 100)))
        no_margin = (spread_cents * no_mult) / 100.0

        # 4. 가격 스큐 (Price Skewing) 유지
        inventory_diff = self.inventory_manager.inventory.net_exposure_shares
        skew_adjustment = (inventory_diff / 1000) * 0.005
        
        yes_bid_price = self.ceil_to_tick(yes_mid - skew_adjustment - yes_margin, tick_size)
        no_bid_price = self.ceil_to_tick(no_mid + skew_adjustment - no_margin, tick_size)

        # 6. Quote 객체 생성
        yes_shares = self.inventory_manager.get_quote_size_yes(final_shares)
        no_shares = self.inventory_manager.get_quote_size_no(final_shares)

        yes_quote = Quote("BUY", yes_bid_price, yes_shares, market_id, yes_token_id) \
            if yes_mid > 0 and self.inventory_manager.can_quote_yes(yes_shares) and 0.01 < yes_bid_price < 0.99 else None

        no_quote = Quote("BUY", no_bid_price, no_shares, market_id, no_token_id) \
            if no_mid > 0 and self.inventory_manager.can_quote_no(no_shares) and 0.01 < no_bid_price < 0.99 else None

        return (yes_quote, no_quote)

    def should_trim_quotes(self, time_to_close_hours: float) -> bool:
        """마감 임박 시 리스크 방지를 위해 주문을 중단합니다."""
        # 설정파일의 avoid_near_expiry_hours와 연동 가능
        return time_to_close_hours < 1.0


