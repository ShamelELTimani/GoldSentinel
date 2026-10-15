import { useEffect, useRef } from 'react'
import { AreaSeries, CandlestickSeries, ColorType, createChart, HistogramSeries, LineSeries, type IChartApi, type UTCTimestamp } from 'lightweight-charts'
import type { Market } from './types'

export function MarketChart({ market, showEma, showZones }: {market: Market; showEma: boolean; showZones: boolean}) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  useEffect(() => {
    if (!host.current) return
    const instance = createChart(host.current, { autoSize: true, layout: { background: {type: ColorType.Solid, color: '#0b0e13'}, textColor: '#79808d', fontFamily: 'Inter, sans-serif'}, grid: {vertLines: {color: '#171b22'}, horzLines: {color: '#171b22'}}, rightPriceScale: {borderColor: '#262b35'}, timeScale: {borderColor: '#262b35', timeVisible: true, secondsVisible: false}, crosshair: {vertLine: {color: '#b48b36'}, horzLine: {color: '#b48b36'}} })
    chart.current = instance
    const candles = instance.addSeries(CandlestickSeries, { upColor: '#2fd59b', downColor: '#f0646a', borderVisible: false, wickUpColor: '#2fd59b', wickDownColor: '#f0646a' })
    candles.setData(market.candles.map(c => ({time: Math.floor(new Date(c.timestamp).getTime()/1000) as UTCTimestamp, open:c.open, high:c.high, low:c.low, close:c.close})))
    const volume = instance.addSeries(HistogramSeries, {priceFormat: {type:'volume'}, priceScaleId: '', color: '#343a46'})
    volume.priceScale().applyOptions({scaleMargins: {top: .82, bottom: 0}})
    volume.setData(market.candles.map(c => ({time: Math.floor(new Date(c.timestamp).getTime()/1000) as UTCTimestamp, value:c.volume, color: c.close >= c.open ? '#183e35' : '#44292d'})))
    if (showEma) {
      const ema20 = instance.addSeries(LineSeries, {color:'#e8b44c', lineWidth:1, priceLineVisible:false, lastValueVisible:false})
      const ema50 = instance.addSeries(LineSeries, {color:'#6c7cd8', lineWidth:1, priceLineVisible:false, lastValueVisible:false})
      ema20.setData(market.candles.map((c,i) => ({time:Math.floor(new Date(c.timestamp).getTime()/1000) as UTCTimestamp, value:market.indicators.ema20[i]})))
      ema50.setData(market.candles.map((c,i) => ({time:Math.floor(new Date(c.timestamp).getTime()/1000) as UTCTimestamp, value:market.indicators.ema50[i]})))
    }
    if (showZones) market.zones.slice(0, 5).forEach(z => {
      const area = instance.addSeries(AreaSeries, {lineColor:z.direction==='BUY'?'#2fd59b':'#f0646a', topColor:z.direction==='BUY'?'rgba(47,213,155,.10)':'rgba(240,100,106,.10)', bottomColor:'rgba(0,0,0,0)', lineWidth:1, priceLineVisible:false, lastValueVisible:false})
      area.setData(market.candles.slice(-90).map(c => ({time:Math.floor(new Date(c.timestamp).getTime()/1000) as UTCTimestamp, value:(z.upper+z.lower)/2})))
    })
    instance.timeScale().fitContent()
    return () => instance.remove()
  }, [market, showEma, showZones])
  return <div className="chart" ref={host} />
}

