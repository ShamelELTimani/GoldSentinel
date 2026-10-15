export type Mode = 'FULL_AUTO' | 'CONFIRMATION' | 'SCANNER'
export type Profile = 'CONSERVATIVE' | 'BALANCED' | 'OPPORTUNISTIC'
export type Timeframe = 'M1' | 'M5' | 'M15' | 'M30' | 'H1' | 'H4' | 'D1' | 'W1' | 'MN1'

export interface RiskSettings {
  max_risk_per_trade_pct: number; max_combined_risk_pct: number; max_positions: number
  daily_drawdown_stop_pct: number; consecutive_loss_limit: number; cooldown_minutes: number
  preferred_min_rr: number; news_policy: 'CONSERVATIVE' | 'BALANCED' | 'FLEXIBLE'
}
export interface Runtime { mode: Mode; strategy_profile: Profile; risk: RiskSettings; paused: boolean; emergency_stop: boolean; armed: boolean }
export interface Quote { bid: number; ask: number; timestamp: string; source: string }
export interface QuoteHealth { state: 'LIVE'|'QUIET'|'STALE'|'DISCONNECTED'; stale: boolean; age_seconds: number|null; error: string|null }
export interface QuoteSnapshot extends QuoteHealth { connected: boolean; quote: Quote|null; spread: number|null }
export interface Candle { timestamp: string; open: number; high: number; low: number; close: number; volume: number; spread: number; complete: boolean }
export interface Zone { id: string; direction: 'BUY' | 'SELL'; timeframe: Timeframe; lower: number; upper: number; invalidation: number; entry: number; stop_loss: number; take_profit: number; reward_risk: number; quality_score: number; touches: number; broken: boolean; relevance: number; evidence: string[]; explanation: string }
export interface Position { ticket: string; symbol: string; side: 'BUY' | 'SELL'; volume: number; entry: number; current_price: number; stop_loss: number; take_profit: number; pnl: number; initial_risk: number; opened_at: string }
export interface ReadinessCheck { id: string; label: string; ready: boolean; blocking: boolean; detail: string }
export interface Readiness { ready: boolean; can_arm: boolean; blockers: string[]; checks: ReadinessCheck[] }
export interface NewsRestriction { status:string; factor:number; block:boolean; reason:string }
export interface NewsStatus { provider:string; provider_state:string; configured:boolean; available:boolean; verified_empty:boolean; fresh:boolean; last_attempt:string|null; last_success:string|null; last_refresh:string|null; retry_at:string|null; error:string|null; date_range:{country:string;from:string;to:string}; next_high_impact:NewsEvent|null; seconds_to_next_high_impact:number|null; recent_releases:NewsEvent[]; restriction:NewsRestriction; events:NewsEvent[]; headline_provider:string; headline_state:string; headline_configured:boolean; headline_available:boolean; headline_verified_empty:boolean; headline_last_attempt:string|null; headline_last_success:string|null; headline_retry_at:string|null; headline_error:string|null; headlines:Headline[] }
export interface Status { connected: boolean; broker: string; data_source: string; symbol: string; quote: Quote; quote_health: QuoteHealth; spread: number; account: {balance: number; equity: number; free_margin: number; margin: number; leverage: number; floating_pnl: number; is_demo: boolean}; runtime: Runtime; positions: Position[]; pending_orders: unknown[]; open_risk: number; remaining_risk: number; latest_candidate: Candidate | null; readiness: Readiness; server_time: string; news: NewsStatus }
export interface NewsEvent { id: string; title: string; timestamp: string; impact: string; currency:string; country?:string|null; actual?:string|number|null; forecast?:string|number|null; previous?:string|number|null; unit?:string|null; surprise?:number|null; surprise_pct?:number|null; source: string }
export interface Headline { title: string; url?: string|null; source: string; timestamp?: string|null }
export interface Market { timeframe: Timeframe; source:string; candles: Candle[]; zones: Zone[]; quality: {valid: boolean; stale: boolean; gaps: string[]; blocking_gaps:string[]; expected_gaps:string[]; errors: string[]; warnings:string[]; last_timestamp:string|null; age_seconds:number|null}; structure: {trend: string; bos?: string; choch?: string; liquidity_sweep: boolean}; indicators: {ema20: number[]; ema50: number[]; latest: {ema20: number; ema50: number; rsi: number; atr: number; volatility_regime: string}} }
export interface LiveMarket extends QuoteSnapshot { symbol:string; timeframe:Timeframe; candles:Candle[] }
export interface Candidate { id: string; action: 'BUY'|'SELL'|'NO_TRADE'; timeframe: Timeframe; entry: number; stop_loss: number; take_profit: number; volume: number; risk_amount: number; risk_pct: number; reward_risk: number; quality_score: number; news_status: string; rationale: string[]; decision_class:'TRADE'|'MARKET'|'RISK'|'OPERATIONAL'; blockers:string[]; status: string; spread_at_signal:number; max_price_deviation:number; evaluated_zones:number }

