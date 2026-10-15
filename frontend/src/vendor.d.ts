declare module 'lightweight-charts' {
  export type UTCTimestamp = number & { readonly __brand: unique symbol }
  export type IChartApi = any
  export const ColorType: any
  export const CandlestickSeries: any
  export const HistogramSeries: any
  export const LineSeries: any
  export const AreaSeries: any
  export function createChart(container: HTMLElement, options?: any): IChartApi
}

declare module 'recharts' {
  import type { ComponentType } from 'react'
  export const Area: ComponentType<any>
  export const AreaChart: ComponentType<any>
  export const Bar: ComponentType<any>
  export const BarChart: ComponentType<any>
  export const CartesianGrid: ComponentType<any>
  export const ResponsiveContainer: ComponentType<any>
  export const Tooltip: ComponentType<any>
  export const XAxis: ComponentType<any>
  export const YAxis: ComponentType<any>
}
