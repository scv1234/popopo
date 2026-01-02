import asyncio
import aiohttp
import sqlite3
import json
import logging
import time  # <--- 이 줄을 반드시 추가하세요!
import pandas as pd
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

class HoneypotService:
    def __init__(self, settings=None):
        self.params = {
            "min_daily_reward_usd": settings.min_daily_reward_usd if settings else 10,
            "max_existing_depth_usd": getattr(settings, 'max_existing_depth_usd', 100000),
            "min_mid_price": getattr(settings, 'min_mid_price', 0.15),
            "max_mid_price": getattr(settings, 'max_mid_price', 0.85),
            "max_order_size_shares": 500,
            "avoid_near_expiry_hours": 10,
            "max_concurrent": 40,
            "limit": 500,
            "max_pages_per_sort": 10        # 정렬당 10페이지 (500개 * 10 = 5,000개)
        }
        self.GAMMA_API = "https://gamma-api.polymarket.com/markets"
        self.CLOB_API = "https://clob.polymarket.com"
        self._session = None # [필수 추가] AttributeError 해결을 위한 초기화

    async def get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        # [수정] 세션이 존재할 때만 닫도록 안전하게 처리
        if hasattr(self, '_session') and self._session:
            await self._session.close()

    async def get_market(self, session, market_id: str):
        """마켓 상세 메타데이터 조회 (Gamma API)"""
        async with session.get(f"{self.GAMMA_API}/{market_id}") as res:
            return await res.json() if res.status == 200 else {}

    async def get_orderbook(self, session, token_id: str):
        """실시간 오더북 조회 (CLOB API)"""
        async with session.get(f"{self.CLOB_API}/book?token_id={token_id}") as res:
            return await res.json() if res.status == 200 else {}

    async def get_price_history(self, session, token_id: str):
        """최근 24시간 가격 히스토리 조회 (CLOB API)"""
        start_ts = int(time.time()) - (24 * 60 * 60)
        # 파라미터 규격: market={token_id}, startTs={timestamp}, fidelity=60(1시간 단위)
        url = f"{self.CLOB_API}/prices-history?market={token_id}&startTs={start_ts}&fidelity=60"
        async with session.get(url) as res:
            if res.status == 200:
                data = await res.json()
                return data.get('history', []) if isinstance(data, dict) else data
            return []

    # --- DB 저장 로직 (클래스 내부 메서드로 이동 및 수정) ---
    def update_honeypot_cache(self, markets):
        try:
            conn = sqlite3.connect('bot_data.db')
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS honeypots (
                    id TEXT PRIMARY KEY,
                    data TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            cursor.execute('DELETE FROM honeypots')
            for market in markets:
                # _calculate_ts_score에서 반환한 키인 'market_id'를 사용해야 함
                cursor.execute('INSERT INTO honeypots (id, data) VALUES (?, ?)', 
                               (market['market_id'], json.dumps(market)))
            
            conn.commit()
            conn.close()
            print(f"💾 {len(markets)}개 마켓 정보가 DB에 캐싱되었습니다.")
        except Exception as e:
            print(f"❌ DB 저장 중 오류 발생: {e}")    

    async def get_market_data_complete(self, session, market, semaphore):
        condition_id = market.get("conditionId")
        clob_token_ids_raw = market.get("clobTokenIds")
        
        if not condition_id or not clob_token_ids_raw:
            return None

        async with semaphore:
            try:
                # Token ID 리스트 변환
                token_ids = json.loads(clob_token_ids_raw) if isinstance(clob_token_ids_raw, str) else clob_token_ids_raw
                if not token_ids: return None

                yes_token = token_ids[0]
                no_token = token_ids[1]                

                # 비동기 병렬 호출
                tasks = [
                    session.get(f"{self.CLOB_API}/rewards/markets/{condition_id}"),
                    self.get_orderbook(session, yes_token), # YES 북
                    self.get_orderbook(session, no_token),  # NO 북
                    self.get_price_history(session, yes_token)
                ]
                responses = await asyncio.gather(*tasks)

                reward_res = responses[0]
                book_yes = responses[1] # YES
                book_no = responses[2]  # NO
                history_data = responses[3]

                # [수정된 부분] .status.json() 이 아니라 .json() 입니다.
                if reward_res.status == 200:
                    reward_json = await reward_res.json() # <- 여기서 에러가 났었습니다.
                    
                    # 24시간 최고/최저가 차이(p 필드) 기반 변동성 계산
                    volatility = self._calculate_volatility(history_data)
                    
                    # 리워드 데이터가 존재하는 경우 최종 점수 계산
                    if reward_json.get("data") and len(reward_json["data"]) > 0:
                        # [수정] 점수 계산 시 YES와 NO 북을 모두 전달하도록 파라미터 확장 가능
                        # 여기서는 일단 기존 구조를 유지하되 YES 북 위주로 계산하고 결과에 NO 정보 추가
                        return self._calculate_ts_score(
                            market, 
                            reward_json["data"][0], 
                            book_yes, # 메인 계산은 YES 기준
                            volatility,
                            book_no=book_no # NO 북 추가 전달
                        )
            except Exception as e:
                # 에러 메시지를 더 자세히 보고 싶다면 아래 주석을 해제하세요
                logger.error(f"상세 에러: {e}") 
                pass
        return None

    def _calculate_volatility(self, history):
        if not history or len(history) < 2:
            return 0.01 # 데이터 부족 시 최소값 반환
    
        # 필드명을 'p'로 접근하여 가격 리스트 생성
        prices = [float(item['p']) for item in history if 'p' in item]
    
        if not prices:
            return 0.01
        
        # 최고가 - 최저가 = 24시간 가격 변동폭
        return max(prices) - min(prices)    

    def _get_effective_depth(self, book_data, spread_usd):
        
        bids = book_data.get("bids", [])
        asks = book_data.get("asks", [])

        if not bids or not asks:
            return 0, 0.5

        # 1. 미드 가격 계산
        # 매수 호가(bids) 중 가장 높은 가격이 Best Bid
        best_bid = max(float(b['price']) for b in bids)
        # 매도 호가(asks) 중 가장 낮은 가격이 Best Ask
        best_ask = min(float(a['price']) for a in asks)
    
        mid_price = (best_bid + best_ask) / 2

        # Polymarket 리워드 기준: Mid * (1 ± spread)
        lower_bound = mid_price - spread_usd 
        upper_bound = mid_price + spread_usd

        effective_depth_usd = 0.0

        # 3. 매수 호가(Bids) 합산
        for bid in bids:
            price = float(bid['price'])
            if price >= lower_bound:
                effective_depth_usd += (price * float(bid['size']))
            else:
                break # 범위를 벗어나면 즉시 중단 (성능 최적화)

        # 4. 매도 호가(Asks) 합산
        for ask in asks:
            price = float(ask['price'])
            if price <= upper_bound:
                effective_depth_usd += (price * float(ask['size']))
            else:
                break # 범위를 벗어나면 즉시 중단

        return effective_depth_usd, mid_price

    def _calculate_ts_score(self, market, reward_info, book, volatility, book_no=None):
        now = datetime.now(timezone.utc)
        
        # 1. 보상 데이터 추출
        rewards_config = reward_info.get("rewards_config", [{}])
        daily_reward = float(rewards_config[0].get("rate_per_day", 0))
        min_inc_size = float(reward_info.get("rewards_min_size", 0))
        raw_spread = float(reward_info.get("rewards_max_spread", 0))
        spread_cents = int(raw_spread)
        spread_usd = spread_cents / 100

        # YES 유동성 및 중간가 계산
        depth_yes, mid_yes = self._get_effective_depth(book, spread_usd)
        
        # [추가] NO 유동성 계산
        depth_no = 0
        
        if book_no:
            depth_no, mid_no = self._get_effective_depth(book_no, spread_usd)

        total_depth = depth_yes + depth_no

        # --- [필터링 로직] ---
        if daily_reward < self.params["min_daily_reward_usd"]: return None
        
        # [수정] 위에서 받아온 정확한 mid 가격으로 필터링 진행
        if mid_yes < self.params["min_mid_price"] or mid_yes > self.params["max_mid_price"]: 
            return None

        if min_inc_size > self.params["max_order_size_shares"]: 
            return None

        # 필터 4: 실효 경쟁자가 너무 많으면 제외
        if total_depth > self.params["max_existing_depth_usd"]: 
            return None

        # 4. 가성비 점수 (Yield Score): $1,000 투입 시 기대 수익 시뮬레이션
        score_base = (daily_reward / max(total_depth, 50)) * 1000

        # 1. 위치 가중치: 0.5에 가까울수록 보너스
        mid_weight = 1 + (1 - abs(mid_yes - 0.5)) * 0.1
    
        # 2. [정교화] 상대적 변동성 페널티: 가격 대비 변동 비율(%)을 고려
        rel_vol = (volatility / mid_yes) if mid_yes > 0 else volatility
        volatility_penalty = 1 + (rel_vol * 15) # 페널티 계수 강화 (10 -> 15)
        
        # 3. 시간 가중치
        try:
            end_time = datetime.fromisoformat(market.get('endDate').replace("Z", "+00:00"))
            hours_left = (end_time - now).total_seconds() / 3600
            if hours_left < self.params["avoid_near_expiry_hours"]: return None
            time_weight = 1 + min(hours_left / 1000, 0.2)
        except: return None
        
        # 최종 점수 산산
        final_score = (score_base * mid_weight * time_weight) / volatility_penalty

        return {
            "market_id": market.get("id"),
            "title": market.get("question"),
            "score": round(final_score, 4),
            "mid_yes": round(mid_yes, 3),
            "mid_no": round(mid_no, 3),
            "reward": round(daily_reward, 2),
            "spread_cents": spread_cents, # [추가] 보상 스프레드 범위
            "depth_yes": round(depth_yes, 2),
            "depth_no": round(depth_no, 2),
            "total_depth": round(total_depth, 2),
            "volatility": round(volatility, 4),
            "rel_vol": round(rel_vol, 4),
            "hours_left": int(hours_left),
            "slug": market.get("slug"),
            "yes_token_id": json.loads(market.get("clobTokenIds", "[]"))[0] if market.get("clobTokenIds") else None,
            "no_token_id": json.loads(market.get("clobTokenIds", "[]"))[1] if market.get("clobTokenIds") else None
        }

    async def scan(self):
        # 5가지 정렬 기준으로 확장
        sorts = ["volume24hr", "liquidity", "createdAt", "newest", "commentCount"]
        unique_markets = {}
        now = datetime.now(timezone.utc)

        async with aiohttp.ClientSession() as session:
            print(f"📡 폴리마켓 광역 전수조사 시작... (기준: {len(sorts)}종 정렬)")
            for sort in sorts:
                for page in range(self.params["max_pages_per_sort"]):
                    offset = page * self.params["limit"]
                    url = f"{self.GAMMA_API}?active=true&closed=false&limit={self.params['limit']}&offset={offset}&order={sort}&dir=desc"
                    
                    async with session.get(url) as res:
                        if res.status != 200: break
                        try: markets = await res.json()
                        except: break
                        if not markets: break
                        
                        for m in markets:
                            # 10시간 필터 미리 적용 (스캔 효율성)
                            end_date_str = m.get('endDate')
                            if not end_date_str: continue
                            try:
                                end_ts = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                                if (end_ts - now).total_seconds() / 3600 < self.params["avoid_near_expiry_hours"]:
                                    continue
                                unique_markets[m.get('id')] = m
                            except: continue
                print(f"   - [{sort:^12}] 완료 (누적 마켓: {len(unique_markets)}개)")

            print(f"\n🔬 {len(unique_markets)}개 시장 후보 정밀 분석 중...")
            semaphore = asyncio.Semaphore(self.params["max_concurrent"])
            tasks = [self.get_market_data_complete(session, m, semaphore) for m in unique_markets.values()]
            results = await asyncio.gather(*tasks)
            
            found = [r for r in results if r is not None]
            found_sorted = sorted(found, key=lambda x: x['score'], reverse=True) # 정렬된 리스트 생성

            if found_sorted:
                self.update_honeypot_cache(found_sorted)
            
            print(f"✅ 최종 {len(found_sorted)}개의 보상 시장을 탐지했습니다.")
            return found_sorted

    async def _process_single_market(self, session, market, semaphore):
        async with semaphore:
            try:
                c_id = market.get("conditionId")
                t_ids = json.loads(market.get("clobTokenIds", "[]"))
                if not c_id or not t_ids: return None

                r_res = await session.get(f"{self.CLOB_API}/rewards/markets/{c_id}")
                b_res = await session.get(f"{self.CLOB_API}/book?token_id={t_ids[0]}")
                h_res = await session.get(f"{self.CLOB_API}/prices-history?token_id={t_ids[0]}&interval=1h")
                
                if r_res.status == 200 and b_res.status == 200 and h_res.status == 200:
                    reward_data = (await r_res.json()).get("data", [])
                    if not reward_data: return None
                    vol = self._calculate_volatility(await h_res.json())
                    return self._calculate_ts_score(market, reward_data[0], await b_res.json(), vol)
            except: pass
            return None            