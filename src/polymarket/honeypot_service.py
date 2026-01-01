import asyncio
import aiohttp
import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

class HoneypotService:
    def __init__(self, settings=None):
        self.params = {
            "min_daily_reward_usd": 10,          # [수정] 하루 배당금이 최소 50달러 이상인 시장만!
            "max_existing_depth_usd": 5000,
            "min_mid_price": 0.15,
            "max_mid_price": 0.85,
            "max_order_size_shares": 500,
            "avoid_near_expiry_hours": 10,
            "max_concurrent": 40,
            "limit": 500,
            "max_pages_per_sort": 10        # 정렬당 10페이지 (500개 * 10 = 5,000개)
        }
        self.GAMMA_API = "https://gamma-api.polymarket.com/markets"
        self.CLOB_API = "https://clob.polymarket.com"

    async def get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session:
            await self._session.close()

    # --- [REST Client 기능 통합] ---
    async def get_market(self, market_id: str):
        session = await self.get_session()
        async with session.get(f"{self.GAMMA_API}/{market_id}") as res:
            return await res.json() if res.status == 200 else {}

    async def get_orderbook(self, token_id: str):
        session = await self.get_session()
        async with session.get(f"{self.CLOB_API}/book?token_id={token_id}") as res:
            return await res.json() if res.status == 200 else {}

    async def get_price_history(self, token_id: str):
        session = await self.get_session()
        url = f"{self.CLOB_API}/prices-history?token_id={token_id}&interval=1h"
        async with session.get(url) as res:
            return await res.json() if res.status == 200 else []    

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
        clob_token_ids = market.get("clobTokenIds")
        if not condition_id or not clob_token_ids: return None

        async with semaphore:
            try:
                token_ids = json.loads(clob_token_ids) if isinstance(clob_token_ids, str) else clob_token_ids
                if not token_ids: return None
                token_id = token_ids[0]
                                
                # 리워드와 오더북 동시 비동기 호출
                reward_task = session.get(f"{self.CLOB_API}/rewards/markets/{condition_id}")
                book_task = session.get(f"{self.CLOB_API}/book?token_id={token_id}")
                history_task = session.get(f"{self.CLOB_API}/prices-history?token_id={token_id}&interval=1h")

                responses = await asyncio.gather(reward_task, book_task, history_task)
                
                if all(r.status == 200 for r in responses):
                    reward_json = await responses[0].json()
                    book_data = await responses[1].json()
                    history_data = await responses[2].json()

                    # 변동성 계산 (가져온 히스토리 기반)
                    volatility = self._calculate_volatility(history_data)
                    
                    if reward_json.get("data") and len(reward_json["data"]) > 0:
                        return self._calculate_ts_score(market, reward_json["data"][0], book_data, volatility)
            except: pass
        return None

    def _calculate_volatility(self, history):
        """최근 가격 이력을 통해 변동성(Price Range)을 계산"""
        if not history or len(history) < 2: return 0.01 # 최소값 방어
        prices = [float(p.get("price", 0.5)) for p in history]
        return max(prices) - min(prices)    

    def _calculate_ts_score(self, market, reward_info, book, volatility):
        now = datetime.now(timezone.utc)
        
        # 1. 보상 데이터 추출
        rewards_config = reward_info.get("rewards_config", [{}])
        # [핵심] 일일 배당금 (rate_per_day) 추출
        daily_reward = float(rewards_config[0].get("rate_per_day", 0))
        min_inc_size = float(reward_info.get("rewards_min_size", 0))
        max_v_spread = float(reward_info.get("rewards_max_spread", 0)) / 100

        # --- [필터링 로직] ---
        
        # 필터 1: 일일 보상액이 설정한 최소치(min_daily_reward_usd)보다 작으면 탈락!
        if daily_reward < self.params["min_daily_reward_usd"]: 
            return None

        # 필터 2: 오더북 미드 가격 범위 체크
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        best_bid = float(bids[0].get("price", 0)) if bids else 0
        best_ask = float(asks[0].get("price", 1)) if asks else 1
        mid = (best_bid + best_ask) / 2 if best_bid > 0 else 0.5
        
        if mid < self.params["min_mid_price"] or mid > self.params["max_mid_price"]: 
            return None

        # 필터 3: 최소 주문 수량 (Min Size) 체크
        if min_inc_size > self.params["max_order_size_shares"]: 
            return None

        # --- [스코어링 로직] ---

        # 실질 경쟁 Depth 계산 (Reward Zone 내의 주문 합계)
        existing_depth_usd = sum(float(b.get('price', 0)) * float(b.get('size', 0)) 
                                 for b in bids if float(b.get('price', 0)) >= mid - max_v_spread)
        existing_depth_usd += sum(float(a.get('price', 1)) * float(a.get('size', 0)) 
                                  for a in asks if float(a.get('price', 1)) <= mid + max_v_spread)

        # 필터 4: 이미 경쟁자가 너무 많으면(유동성이 너무 크면) 내 몫이 적으므로 제외
        if existing_depth_usd > self.params["max_existing_depth_usd"]: 
            return None

        # 1. 위치 가중치: 0.5에 가까울수록 최대 1.1배 가산
        mid_weight = 1 + (1 - abs(mid - 0.5)) * 0.1
    
        # 2. 변동성 페널티: 변동성이 클수록 분모가 커져 점수가 낮아짐
        volatility_penalty = 1 + (volatility * 10)
        
        try:
            end_time = datetime.fromisoformat(market.get('endDate').replace("Z", "+00:00"))
            hours_left = (end_time - now).total_seconds() / 3600
            if hours_left < self.params["avoid_near_expiry_hours"]: return None
            time_weight = 1 + min(hours_left / 168, 0.2)
        except: return None
        
        score_base = daily_reward / max(existing_depth_usd, 10)
    
        # 최종 점수 = (기본점수 * 위치가중치 * 시간가중치) / 변동성페널티
        final_score = (score_base * mid_weight * time_weight) / volatility_penalty

        return {
            "market_id": market.get("id"), # 이식 시 식별을 위해 추가
            "title": market.get("question"),
            "score": round(final_score, 4),
            "mid": round(mid, 3),
            "reward": round(daily_reward, 2),
            "volatility": round(volatility, 4), # 대시보드 표시용 추가
            "max_spread": max_v_spread,        # [필수 추가] 1번 및 4번 방어 로직용 데이터
            "min_size": round(min_inc_size, 1),
            "depth": round(existing_depth_usd, 2),
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
            print(f"✅ 최종 {len(found)}개의 보상 시장을 탐지했습니다.")
            return sorted(found, key=lambda x: x['score'], reverse=True)

            # [추가] 스캔이 끝나면 자동으로 DB 업데이트
            self.update_honeypot_cache(found_sorted)
            
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