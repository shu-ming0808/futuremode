export function sampleExponential(rate: number): number {
  if (rate <= 0) return Infinity
  const u = Math.random()
  return -Math.log(1 - u) / rate
}

/** Box-Muller，取單邊即可（另一邊丟棄比維護快取狀態簡單）。 */
export function sampleNormal(mean = 0, sigma = 1): number {
  const u1 = Math.max(Number.EPSILON, Math.random())
  const u2 = Math.random()
  return mean + sigma * Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2)
}

export function sampleLogNormal(mean: number, sigma: number): number {
  return Math.exp(sampleNormal(mean, sigma))
}

/**
 * Knuth 乘積法。demo 的 lambda 只有數十等級，不需要 PTRS 那種大 lambda 演算法。
 */
export function samplePoisson(lambda: number): number {
  if (lambda <= 0) return 0
  const limit = Math.exp(-lambda)
  let k = 0
  let p = 1
  do {
    k += 1
    p *= Math.random()
  } while (p > limit)
  return k - 1
}
