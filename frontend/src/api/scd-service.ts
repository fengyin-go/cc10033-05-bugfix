import { request } from './client'

/** SCD 单条维度变更提交项 */
export type ScdChange = {
  目标维度: string
  旧版本号: number | string
  属性变更: Record<string, string>
  生效日期?: string
  模拟写入失败?: boolean
}

/** 单个维度的处理结果（与后端 scd_versioning 返回结构对齐） */
export type ScdChangeResult = {
  dim_code: string
  ok: boolean
  switched: boolean
  reason: string
  expected_version?: number
  current_version?: number
  new_version?: number
  effective_date?: string
  effective_start?: string
  old_version_end?: string
  batch_id?: string
  changed_fields?: string[]
  logs?: string[]
}

export type ScdBatchResult = {
  batch_id: string
  strategy_name: string
  scd_type: string
  total: number
  replayed_count: number
  applied_count: number
  conflict_count: number
  failure_count: number
  unchanged_count: number
  switched_dimensions: string[]
  conflict_dimensions: string[]
  failed_dimensions: string[]
  results: ScdChangeResult[]
  message?: string
}

export type DimensionVersion = {
  id: number
  维度编码: string
  status: string
  当前版本号: number
  上一版本号?: number | null
  版本生效时间: string
  版本失效时间: string
  当前标记: string
  版本属性?: string
  来源批次ID?: string
  来源策略?: string
  [field: string]: string | number | boolean | null | undefined
}

export type ChangeLogRow = {
  id: number
  变更ID: string
  目标维度: string
  变更字段: string
  旧值: string
  新值: string
  变更时间: string
  变更原因: string
  批次ID?: string
  策略名称?: string
  生效版本号?: number
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

/** 提交一批维度属性变更，逐维度乐观锁落库并返回同一份处理结果。 */
export async function submitScdBatch(
  strategyId: number,
  changes: ScdChange[],
  batchId?: string,
): Promise<ScdBatchResult> {
  const response = await request(`/api/scd/strategies/${strategyId}/batch`, {
    method: 'POST',
    body: JSON.stringify({ changes, batch_id: batchId }),
  })
  if (!response.ok) throw new Error('SCD 批次提交失败')
  return await response.json()
}

/** 回查同一批次的处理结果。 */
export function fetchScdBatch(batchId: string): Promise<ScdBatchResult> {
  return getJson(`/api/scd/batches/${encodeURIComponent(batchId)}`)
}

/** 读取维度定义的版本记录链。 */
export async function fetchDimensionVersions(dimCode: string): Promise<DimensionVersion[]> {
  const payload = await getJson<{ versions: DimensionVersion[] }>(
    `/api/scd/dimensions/${encodeURIComponent(dimCode)}/versions`,
  )
  return payload.versions
}

/** 变更日志入口：按批次核对实际写入的前后值与生效日期。 */
export async function fetchBatchLogs(batchId: string): Promise<ChangeLogRow[]> {
  const payload = await getJson<{ logs: ChangeLogRow[] }>(
    `/api/scd/batches/${encodeURIComponent(batchId)}/logs`,
  )
  return payload.logs
}

/** 按维度回查变更日志，定位失败时可据此找到原策略。 */
export async function fetchDimensionLogs(dimCode: string): Promise<ChangeLogRow[]> {
  const payload = await getJson<{ logs: ChangeLogRow[] }>(
    `/api/scd/dimensions/${encodeURIComponent(dimCode)}/logs`,
  )
  return payload.logs
}
