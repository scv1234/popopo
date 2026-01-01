import asyncio
import aiohttp
import json
from datetime import datetime, timezone

class HoneypotScanner:
    def __init__(self):
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

    async def get_market_data_complete(self, session, market, semaphore):
        condition_id = market.get("conditionId")
        clob_token_ids = market.get("clobTokenIds")
        if not condition_id or not clob_token_ids: return None

        async with semaphore:
            try:
                if isinstance(clob_token_ids, str):
                    token_ids = json.loads(clob_token_ids)
                else:
                    token_ids = clob_token_ids
                
                if not token_ids: return None
                token_id = token_ids[0]
                
                # 리워드와 오더북 동시 비동기 호출
                reward_task = session.get(f"{self.CLOB_API}/rewards/markets/{condition_id}")
                book_task = session.get(f"{self.CLOB_API}/book?token_id={token_id}")
                responses = await asyncio.gather(reward_task, book_task)
                
                if all(r.status == 200 for r in responses):
                    reward_json = await responses[0].json()
                    book_data = await responses[1].json()
                    
                    if reward_json.get("data") and len(reward_json["data"]) > 0:
                        return self._calculate_ts_score(market, reward_json["data"][0], book_data)
            except: pass
        return None

    def _calculate_ts_score(self, market, reward_info, book):
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
        eligible_bid_min = mid - max_v_spread
        eligible_ask_max = mid + max_v_spread
        existing_depth_usd = 0
        for b in bids:
            p = float(b.get('price', 0))
            if p >= eligible_bid_min: existing_depth_usd += p * float(b.get('size', 0))
        for a in asks:
            p = float(a.get('price', 1))
            if p <= eligible_ask_max: existing_depth_usd += p * float(a.get('size', 0))

        # 필터 4: 이미 경쟁자가 너무 많으면(유동성이 너무 크면) 내 몫이 적으므로 제외
        if existing_depth_usd > self.params["max_existing_depth_usd"]: 
            return None

        # TS 공식: 점수 = (일일보상 / 경쟁Depth) * Mid가중치 * 시간가중치
        score_base = daily_reward / max(existing_depth_usd, 10)
        mid_weight = 1 + (1 - abs(mid - 0.5)) * 0.1
        
        try:
            end_time = datetime.fromisoformat(market.get('endDate').replace("Z", "+00:00"))
            hours_left = (end_time - now).total_seconds() / 3600
            if hours_left < self.params["avoid_near_expiry_hours"]: return None
            time_weight = 1 + min(hours_left / 168, 0.2)
        except: return None
        
        final_score = score_base * mid_weight * time_weight

        return {
            "title": market.get("question"),
            "score": round(final_score, 4),
            "mid": round(mid, 3),
            "reward": round(daily_reward, 2),    # 일일 배당금 표시
            "min_size": round(min_inc_size, 1),
            "depth": round(existing_depth_usd, 2),
            "hours_left": int(hours_left),
            "slug": market.get("slug")
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