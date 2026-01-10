# src/market_maker/quote_engine.py
from __future__ import annotations

from dataclasses import dataclass
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
        # 한쪽이 팔렸을 때의 가격을 기억하여 반대쪽 매도 마지노선을 계산합니다.
        self.last_sold_prices = {"YES": 0.0, "NO": 0.0}

    def ceil_to_tick(self, price: float, tick_size: float) -> float:
        """틱 사이즈에 맞춰 가격을 올림 처리합니다 (매도 호가 최적화)"""
        if tick_size <= 0: return round(price, 2)
        precision = int(-math.log10(tick_size))
        normalized_price = round(price / tick_size, 8)
        return round(math.ceil(normalized_price) * tick_size, precision)

    def generate_quotes(
        self, 
        market_id: str, 
        yes_best_bid: float, yes_best_ask: float,
        no_best_bid: float, no_best_ask: float,
        yes_token_id: str, no_token_id: str, 
        tick_size: float = 0.01,
    ) -> tuple[Quote | None, Quote | None]:
        """
        [무위험 리워드 파밍 전략]
        1. 민팅된 재고(Yes, No)가 모두 있을 때: 합계 $1.01 이상으로 양방향 SELL
        2. 한쪽만 있을 때 (Leg Risk 발생): 원금 회수선($1.0 - 판매가) 이상으로 SELL
        """
        inv = self.inventory_manager.inventory
        yes_qty = inv.yes_position
        no_qty = inv.no_position

        # 재고가 아예 없으면 민팅(Split)이 필요하므로 쿼트 생성 안 함
        if yes_qty <= 0 and no_qty <= 0:
            return (None, None)

        yes_quote = None
        no_quote = None

        # --- Case 1: 양방향 재고가 모두 있는 경우 (정상 파밍 모드) ---
        if yes_qty > 0 and no_qty > 0:
            # 원금($1.0)에 수익($0.01)을 더한 합계 타겟 설정
            target_total = 1.01 
            
            # 중간가(Midpoint) 계산
            yes_mid = (yes_best_bid + yes_best_ask) / 2.0
            no_mid = (no_best_bid + no_best_ask) / 2.0

            # 보상 최적화 가격: 중간가보다 한 틱 위 혹은 Best Ask 점유
            y_price = max(self.ceil_to_tick(yes_mid + tick_size, tick_size), yes_best_ask)
            n_price = max(self.ceil_to_tick(no_mid + tick_size, tick_size), no_best_ask)

            # 가격 검증: 두 가격의 합이 타겟보다 낮으면 조정
            if (y_price + n_price) < target_total:
                # 부족분만큼 더 비싼 쪽의 가격을 올림
                diff = target_total - (y_price + n_price)
                if y_price > n_price: y_price += diff
                else: n_price += diff

            yes_quote = Quote("SELL", self.ceil_to_tick(y_price, tick_size), yes_qty, market_id, yes_token_id)
            no_quote = Quote("SELL", self.ceil_to_tick(n_price, tick_size), no_qty, market_id, no_token_id)

        # --- Case 2: Yes만 남은 경우 (No가 먼저 팔린 상황) ---
        elif yes_qty > 0:
            sold_price_no = self.last_sold_prices["NO"]
            # 원금 사수 마지노선: $1.0 - No 판매가
            # 예: No를 0.61에 팔았다면 Yes는 0.39 이상이면 무위험
            min_recovery_price = max(1.0 - sold_price_no, 0.01)
            
            # 시장에서 가장 경쟁력 있는 가격(Best Ask)으로 탈출 시도하되 마지노선 준수
            escape_price = max(yes_best_ask, min_recovery_price)
            
            yes_quote = Quote("SELL", self.ceil_to_tick(escape_price, tick_size), yes_qty, market_id, yes_token_id)
            logger.info("Leg Risk Recovery: Selling YES", min_price=min_recovery_price, target=escape_price)

        # --- Case 3: No만 남은 경우 (Yes가 먼저 팔린 상황) ---
        elif no_qty > 0:
            sold_price_yes = self.last_sold_prices["YES"]
            min_recovery_price = max(1.0 - sold_price_yes, 0.01)
            
            escape_price = max(no_best_ask, min_recovery_price)
            
            no_quote = Quote("SELL", self.ceil_to_tick(escape_price, tick_size), no_qty, market_id, no_token_id)
            logger.info("Leg Risk Recovery: Selling NO", min_price=min_recovery_price, target=escape_price)

        return yes_quote, no_quote

    def update_last_sold_price(self, token_type: str, price: float):
        """체결 시 판매가를 기록 (main.py의 핸들러에서 호출 필요)"""
        self.last_sold_prices[token_type.upper()] = price
