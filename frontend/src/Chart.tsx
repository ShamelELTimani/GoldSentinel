import { useEffect, useRef } from 'react'
import { CandlestickSeries, ColorType, createChart, HistogramSeries, LineSeries, LineStyle, type IChartApi, type IPriceLine, type ISeriesApi, type UTCTimestamp } from 'lightweight-charts'
import type { Market } from './types'

const time = (value:string) => Math.floor(new Date(value).getTime()/1000) as UTCTimestamp

export function MarketChart({ market, showEma, showZones, resetKey=0 }: {market: Market; showEma: boolean; showZones: boolean; resetKey?:number}) {
  const host = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const candleSeries = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeSeries = useRef<ISeriesApi<'Histogram'> | null>(null)
  const ema20Series = useRef<ISeriesApi<'Line'> | null>(null)
  const ema50Series = useRef<ISeriesApi<'Line'> | null>(null)
  const zoneLines = useRef<IPriceLine[]>([])
  const lastFrame = useRef<string>('')
  const lastFirstTime = useRef<number>(0)
  const lastLength = useRef(0)
  const initialized = useRef(false)

  useEffect(() => {
    if (!host.current) return
    const instance = createChart(host.current, {
      autoSize: true,
      layout: { background: {type: ColorType.Solid, color: '#0b0e13'}, textColor: '#79808d', fontFamily: 'Inter, sans-serif'},
      grid: {vertLines: {color: '#171b22'}, horzLines: {color: '#171b22'}},
      rightPriceScale: {borderColor: '#262b35'},
      timeScale: {borderColor: '#262b35', timeVisible: true, secondsVisible: false},
      crosshair: {vertLine: {color: '#b48b36'}, horzLine: {color: '#b48b36'}},
      handleScroll: {mouseWheel:true, pressedMouseMove:true, horzTouchDrag:true, vertTouchDrag:false},
      handleScale: {axisPressedMouseMove:true, mouseWheel:true, pinch:true},
    })
    chart.current = instance
    candleSeries.current = instance.addSeries(CandlestickSeries, { upColor: '#2fd59b', downColor: '#f0646a', borderVisible: false, wickUpColor: '#2fd59b', wickDownColor: '#f0646a' })
    volumeSeries.current = instance.addSeries(HistogramSeries, {priceFormat: {type:'volume'}, priceScaleId: '', color: '#343a46'})
    volumeSeries.current.priceScale().applyOptions({scaleMargins: {top: .82, bottom: 0}})
    ema20Series.current = instance.addSeries(LineSeries, {color:'#e8b44c', lineWidth:1, priceLineVisible:false, lastValueVisible:false})
    ema50Series.current = instance.addSeries(LineSeries, {color:'#6c7cd8', lineWidth:1, priceLineVisible:false, lastValueVisible:false})
    return () => { instance.remove(); chart.current=null; candleSeries.current=null; volumeSeries.current=null; ema20Series.current=null; ema50Series.current=null }
  }, [])

  useEffect(() => {
    const instance=chart.current, candles=candleSeries.current, volume=volumeSeries.current
    if (!instance || !candles || !volume || !market.candles.length) return
    const candleData=market.candles.map(c=>({time:time(c.timestamp),open:c.open,high:c.high,low:c.low,close:c.close}))
    const volumeData=market.candles.map(c=>({time:time(c.timestamp),value:c.volume,color:c.close>=c.open?'#183e35':'#44292d'}))
    const first=Number(candleData[0].time), sameWindow=lastFrame.current===market.timeframe&&lastFirstTime.current===first&&lastLength.current===candleData.length
    if (sameWindow) {
      candles.update(candleData[candleData.length-1])
      volume.update(volumeData[volumeData.length-1])
    } else {
      const visible=lastFrame.current===market.timeframe?instance.timeScale().getVisibleLogicalRange():null
      candles.setData(candleData); volume.setData(volumeData)
      if (visible) instance.timeScale().setVisibleLogicalRange(visible)
    }
    const ema20=market.candles.flatMap((c,i)=>Number.isFinite(market.indicators.ema20[i])?[{time:time(c.timestamp),value:market.indicators.ema20[i]}]:[])
    const ema50=market.candles.flatMap((c,i)=>Number.isFinite(market.indicators.ema50[i])?[{time:time(c.timestamp),value:market.indicators.ema50[i]}]:[])
    ema20Series.current?.setData(ema20); ema50Series.current?.setData(ema50)
    if (!initialized.current || lastFrame.current!==market.timeframe) { instance.timeScale().fitContent(); initialized.current=true }
    lastFrame.current=market.timeframe; lastFirstTime.current=first; lastLength.current=candleData.length
  }, [market.candles, market.indicators.ema20, market.indicators.ema50, market.timeframe])

  useEffect(()=>{ ema20Series.current?.applyOptions({visible:showEma}); ema50Series.current?.applyOptions({visible:showEma}) },[showEma])

  useEffect(()=>{
    const candles=candleSeries.current
    if(!candles)return
    zoneLines.current.forEach(line=>candles.removePriceLine(line)); zoneLines.current=[]
    if(!showZones)return
    market.zones.slice(0,5).forEach(zone=>{
      const color=zone.direction==='BUY'?'#2fd59b':'#f0646a'
      zoneLines.current.push(candles.createPriceLine({price:zone.lower,color,lineWidth:1,lineStyle:LineStyle.Dashed,axisLabelVisible:false,title:''}))
      zoneLines.current.push(candles.createPriceLine({price:zone.upper,color,lineWidth:1,lineStyle:LineStyle.Dotted,axisLabelVisible:true,title:zone.direction}))
    })
  },[market.zones,showZones])

  useEffect(()=>{ if(resetKey>0)chart.current?.timeScale().fitContent() },[resetKey])
  return <div className="chart" ref={host} />
}
