import asyncio
import aiohttp
import sqlite3
import json
import logging
import math
import time  # <--- 이 줄을 반드시 추가하세요!
import pandas as pd
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

class HoneypotService:
    def __init__(self, settings=None):
        self.params = {
            "min_daily_reward_usd": settings.min_daily_reward_usd if settings else 10,
            "max_existing_depth_usd": getattr(settings, 'max_existing_depth_usd', 5000),
            "min_mid_price": getattr(settings, 'min_mid_price', 0.15),
            "max_mid_price": getattr(settings, 'max_mid_price', 0.85),
            "max_order_size_shares": 200,
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

        # 매수(Bids): 비싼 가격 -> 싼 가격 (내림차순)
        # 매수(Bids): 비싼 가격 -> 싼 가격 (내림차순)
        bids.sort(key=lambda x: float(x['price']), reverse=True)
        # 매도(Asks): 싼 가격 -> 비싼 가격 (오름차순)
        asks.sort(key=lambda x: float(x['price']))

        # 1. 미드 가격 계산 (정렬 후에는 0번째 인덱스가 Best Price)
        best_bid = float(bids[0]['price'])
        best_ask = float(asks[0]['price'])
    
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
        daily_reward = float(reward_info.get("rewards_daily_rate") or 0)
        if daily_reward == 0:
            configs = reward_info.get("rewards_config", [{}])
            daily_reward = float(configs[0].get("rate_per_day") or 0)
            
        raw_spread = float(reward_info.get("rewards_max_spread", 3))
        spread_cents = int(raw_spread)
        spread_usd = spread_cents / 100.0
        min_size = float(reward_info.get("rewards_min_size", 20))

        # [추가] 🚨 스프레드 안전 장치: 시장 스프레드가 리워드 범위의 절반을 넘으면 위험
        b_yes = sorted(book.get("bids", []), key=lambda x: float(x['price']), reverse=True)
        a_yes = sorted(book.get("asks", []), key=lambda x: float(x['price']))
        
        if not b_yes or not a_yes:
            return None
            
        market_spread = float(a_yes[0]['price']) - float(b_yes[0]['price'])
        if market_spread > (spread_usd * 2):
            return None

        # YES 유동성 및 중간가 계산
        depth_yes, mid_yes = self._get_effective_depth(book, spread_usd)
        depth_no, mid_no = (self._get_effective_depth(book_no, spread_usd) if book_no else (0, 0.5))
        total_depth = depth_yes + depth_no

        # --- [필터링 로직] ---
        if daily_reward < self.params["min_daily_reward_usd"]: return None
        
        # [수정] 위에서 받아온 정확한 mid 가격으로 필터링 진행
        if not (self.params["min_mid_price"] <= mid_yes <= self.params["max_mid_price"]): return None

        if min_size > self.params["max_order_size_shares"]: return None

        # 필터 4: 실효 경쟁자가 너무 많으면 제외
        if total_depth > self.params["max_existing_depth_usd"]: 
            return None

        # (1) Base Yield: $1,000 투입 시 지분 대비 수익 (최소 분모 $1,000 설정)
        yield_score = (daily_reward / max(total_depth, 1000)) * 1000

        # (2) Price Safety: 0.5(50:50) 근처일 때 가장 안전 (가우시안 정규분포)
        dist_from_mid = abs(mid_yes - 0.5)
        # sigma=0.15: 0.5일 때 1.0, 0.7 or 0.3일 때 약 0.4
        price_safety = math.exp(- (dist_from_mid ** 2) / (2 * (0.15 ** 2)))

        # (3) Volatility Safety: 변동성이 작을수록 안전 (역수 감쇠)
        vol_safety = 1 / (1 + (volatility * 50))

        # (4) Time & Liquidity: 시간 및 탈출 가능성 가중치
        try:
            end_time = datetime.fromisoformat(market.get('endDate').replace("Z", "+00:00"))
            hours_left = (end_time - now).total_seconds() / 3600
            if hours_left < self.params["avoid_near_expiry_hours"]: return None
            time_score = 1 + (math.log10(hours_left + 1) * 0.1) 
        except:
            time_score = 1.0

        # 🏆 최종 점수 합산
        final_score = yield_score * price_safety * vol_safety * time_score * 10

        clob_token_ids = market.get("clobTokenIds")
        token_ids = json.loads(clob_token_ids) if isinstance(clob_token_ids, str) else clob_token_ids

        return {
            "market_id": market.get("conditionId"),
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
            "metrics": {
                "yield": round(yield_score, 2),
                "safe_p": round(price_safety, 2),
                "safe_v": round(vol_safety, 2)
            },
            "hours_left": hours_left,
            "slug": market.get("slug"),
            "yes_token_id": token_ids[0] if token_ids else None,
            "no_token_id": token_ids[1] if token_ids else None
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

            if found_sorted or not unique_markets: # 데이터가 아예 없을 때도 캐시 갱신
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