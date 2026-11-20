<template>
  <section class="scd-panel" data-testid="scd-batch-panel">
    <header class="page-head">
      <div>
        <h3>SCD 属性变更落库</h3>
        <p class="page-desc">
          一次可提交多组维度变更：按旧版本号做乐观锁校验，真正切换的范围与实际写入的前后值逐条可核对。
        </p>
      </div>
    </header>

    <div class="scd-form">
      <label class="scd-field">
        <span>SCD策略</span>
        <select v-model="strategyId" data-testid="scd-strategy-select">
          <option v-for="row in strategies" :key="String(row.id)" :value="Number(row.id)">
            {{ String(row['策略名称']) }}（{{ String(row['目标维度']) }} / {{ String(row['SCD类型']) }}）
          </option>
        </select>
      </label>
      <label class="scd-field">
        <span>批次ID（留空自动生成，重复提交同ID即幂等回放）</span>
        <input v-model="batchId" data-testid="scd-batch-id" placeholder="可选" />
      </label>
    </div>

    <div class="scd-groups">
      <article v-for="(group, index) in groups" :key="index" class="scd-group" :data-testid="`scd-group-${index}`">
        <h4>第{{ index + 1 }}组变更</h4>
        <label class="scd-field">
          <span>目标维度</span>
          <input v-model="group.dimCode" data-testid="scd-dim" placeholder="如 DIM_PRODUCT" />
        </label>
        <label class="scd-field">
          <span>旧版本号</span>
          <input v-model="group.expectedVersion" data-testid="scd-old-version" placeholder="如 1" />
        </label>
        <label class="scd-field">
          <span>属性变更（格式：字段=值，多个用逗号分隔）</span>
          <input v-model="group.attrsText" data-testid="scd-attrs" placeholder="product_name=新名称, product_category=饮料" />
        </label>
        <label class="scd-field">
          <span>生效日期</span>
          <input v-model="group.effectiveDate" data-testid="scd-effective-date" placeholder="YYYY-MM-DD" />
        </label>
        <label class="scd-check">
          <input v-model="group.injectFailure" type="checkbox" data-testid="scd-inject-failure" />
          <span>模拟写入失败（验证失败不产生已生效记录）</span>
        </label>
      </article>
    </div>

    <div class="page-actions">
      <button class="btn primary" type="button" data-testid="scd-submit" :disabled="submitting" @click="submit">
        {{ submitting ? '提交中…' : '提交变更批次' }}
      </button>
      <button class="btn" type="button" data-testid="scd-refill-demo" @click="fillDemo">填入演示两组（一匹配一冲突）</button>
      <button v-if="lastBatchId" class="btn ghost" type="button" data-testid="scd-recheck" @click="recheck">
        回查批次 {{ lastBatchId }}
      </button>
    </div>

    <p v-if="errorMessage" class="error-text" data-testid="scd-error">{{ errorMessage }}</p>

    <div v-if="batch" class="scd-result" data-testid="scd-result">
      <h4>处理结果（批次 {{ batch.batch_id }}）</h4>
      <div class="stat-row">
        <article class="stat-card"><span class="stat-label">真正切换</span>
          <strong class="stat-value">{{ batch.applied_count }}</strong></article>
        <article class="stat-card"><span class="stat-label">版本冲突</span>
          <strong class="stat-value">{{ batch.conflict_count }}</strong></article>
        <article class="stat-card"><span class="stat-label">写入失败</span>
          <strong class="stat-value">{{ batch.failure_count }}</strong></article>
        <article class="stat-card"><span class="stat-label">无变化</span>
          <strong class="stat-value">{{ batch.unchanged_count }}</strong></article>
        <article class="stat-card"><span class="stat-label">幂等回放</span>
          <strong class="stat-value">{{ batch.replayed_count }}</strong></article>
      </div>
      <p class="scd-range">
        切换范围：<b data-testid="scd-switched-range">{{ batch.switched_dimensions.join('、') || '无' }}</b>
        ｜冲突范围：<b>{{ batch.conflict_dimensions.join('、') || '无' }}</b>
      </p>
      <table class="data-table">
        <thead>
          <tr><th>维度</th><th>结果</th><th>版本 v旧→v新</th><th>生效日期</th><th>说明</th><th>核对</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in batch.results" :key="row.dim_code" :data-testid="`scd-result-row-${row.dim_code}`">
            <td>{{ row.dim_code }}</td>
            <td>
              <span v-if="row.switched" class="tag tag-ok">已切换</span>
              <span v-else-if="row.ok" class="tag tag-idle">无变化</span>
              <span v-else class="tag tag-fail">未写入</span>
            </td>
            <td>{{ row.current_version ?? '—' }} → {{ row.new_version ?? '—' }}</td>
            <td>{{ row.effective_date || '—' }}</td>
            <td>{{ row.reason }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`scd-check-version-${row.dim_code}`" @click="checkVersions(row.dim_code)">
                维度版本
              </button>
              <button class="link" type="button" :data-testid="`scd-check-logs-${row.dim_code}`" @click="checkDimLogs(row.dim_code)">
                变更日志
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="versions.length" class="scd-versions" data-testid="scd-versions">
      <h4>维度版本记录：{{ versionDim }}</h4>
      <table class="data-table">
        <thead>
          <tr><th>版本号</th><th>当前标记</th><th>生效时间</th><th>失效时间</th><th>版本属性</th><th>来源批次/策略</th></tr>
        </thead>
        <tbody>
          <tr v-for="ver in versions" :key="String(ver.id)" :data-testid="`scd-version-row-${ver.id}`">
            <td>v{{ ver['当前版本号'] }}</td>
            <td>{{ ver['当前标记'] }}</td>
            <td>{{ ver['版本生效时间'] }}</td>
            <td>{{ ver['版本失效时间'] }}</td>
            <td>{{ ver['版本属性'] || '—' }}</td>
            <td>{{ ver['来源批次ID'] || '—' }} / {{ ver['来源策略'] || '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="scd-logs" data-testid="scd-logs">
      <h4>变更日志（仅登记实际写入的前后值）{{ logTitle }}</h4>
      <table class="data-table">
        <thead>
          <tr><th>变更ID</th><th>目标维度</th><th>变更字段</th><th>旧值</th><th>新值</th><th>生效日期</th><th>原策略</th></tr>
        </thead>
        <tbody>
          <tr v-for="log in logs" :key="String(log.id)" :data-testid="`scd-log-row-${log.id}`">
            <td>{{ log['变更ID'] }}</td>
            <td>{{ log['目标维度'] }}</td>
            <td>{{ log['变更字段'] }}</td>
            <td>{{ log['旧值'] }}</td>
            <td>{{ log['新值'] }}</td>
            <td>{{ log['变更时间'] }}</td>
            <td>{{ log['策略名称'] || '—' }}</td>
          </tr>
          <tr v-if="!logs.length">
            <td colspan="7" class="empty-state">该批次没有实际写入的变更日志</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'

import {
  fetchBatchLogs,
  fetchDimensionLogs,
  fetchDimensionVersions,
  fetchScdBatch,
  submitScdBatch,
  type ChangeLogRow,
  type DimensionVersion,
  type ScdBatchResult,
  type ScdChange,
} from '@/api/scd-service'
import type { EntryRow } from '@/data/types'

type Group = {
  dimCode: string
  expectedVersion: string
  attrsText: string
  effectiveDate: string
  injectFailure: boolean
}

const props = defineProps<{ strategies: EntryRow[] }>()

const strategyId = ref<number | null>(props.strategies[0] ? Number(props.strategies[0].id) : null)
const batchId = ref('')
const groups = ref<Group[]>([makeEmptyGroup(), makeEmptyGroup()])
const submitting = ref(false)
const errorMessage = ref('')
const batch = ref<ScdBatchResult | null>(null)
const logs = ref<ChangeLogRow[]>([])
const versions = ref<DimensionVersion[]>([])
const versionDim = ref('')
const logTitle = ref('')

const lastBatchId = computed(() => batch.value?.batch_id ?? '')

function makeEmptyGroup(): Group {
  return { dimCode: '', expectedVersion: '1', attrsText: '', effectiveDate: '', injectFailure: false }
}

function parseAttrs(text: string): Record<string, string> {
  const attrs: Record<string, string> = {}
  text.split(',').map((part) => part.trim()).filter(Boolean).forEach((part) => {
    const [key, ...rest] = part.split('=')
    if (key && rest.length) attrs[key.trim()] = rest.join('=').trim()
  })
  return attrs
}

function buildChanges(): ScdChange[] {
  return groups.value
    .filter((group) => group.dimCode.trim())
    .map((group) => {
      const change: ScdChange = {
        目标维度: group.dimCode.trim(),
        旧版本号: Number(group.expectedVersion) || 1,
        属性变更: parseAttrs(group.attrsText),
      }
      if (group.effectiveDate.trim()) change.生效日期 = group.effectiveDate.trim()
      if (group.injectFailure) change.模拟写入失败 = true
      return change
    })
}

async function submit() {
  errorMessage.value = ''
  if (!strategyId.value) {
    errorMessage.value = '请选择 SCD 策略'
    return
  }
  const changes = buildChanges()
  if (!changes.length) {
    errorMessage.value = '至少填写一组完整变更（目标维度与属性变更）'
    return
  }
  if (changes.some((change) => !Object.keys(change.属性变更).length)) {
    errorMessage.value = '属性变更格式应为 字段=值，多个用逗号分隔'
    return
  }
  submitting.value = true
  try {
    batch.value = await submitScdBatch(strategyId.value, changes, batchId.value || undefined)
    await loadBatchLogs(batch.value.batch_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : 'SCD 批次提交失败'
  } finally {
    submitting.value = false
  }
}

async function recheck() {
  if (!batch.value) return
  errorMessage.value = ''
  try {
    batch.value = await fetchScdBatch(batch.value.batch_id)
    await loadBatchLogs(batch.value.batch_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批次回查失败'
  }
}

async function loadBatchLogs(id: string) {
  logTitle.value = `（批次 ${id}）`
  logs.value = await fetchBatchLogs(id)
}

async function checkVersions(dimCode: string) {
  errorMessage.value = ''
  try {
    versionDim.value = dimCode
    versions.value = await fetchDimensionVersions(dimCode)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '维度版本读取失败'
  }
}

async function checkDimLogs(dimCode: string) {
  errorMessage.value = ''
  try {
    logTitle.value = `（维度 ${dimCode}，含历史记录）`
    logs.value = await fetchDimensionLogs(dimCode)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '变更日志读取失败'
  }
}

function fillDemo() {
  const first = props.strategies[0]
  if (first) strategyId.value = Number(first.id)
  batchId.value = ''
  groups.value = [
    {
      dimCode: 'DIM_PRODUCT', expectedVersion: '1',
      attrsText: 'product_name=演示新产品, product_category=饮料',
      effectiveDate: '2026-11-20', injectFailure: false,
    },
    {
      dimCode: 'DIM_SUPPLIER', expectedVersion: '9',
      attrsText: 'supplier_grade=S',
      effectiveDate: '2026-11-20', injectFailure: false,
    },
  ]
}
</script>

<style scoped>
.scd-panel {
  margin-top: 24px;
  background: #f8fafc;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px;
}
.scd-form,
.scd-groups {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 12px;
}
.scd-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1 1 240px;
  font-size: 13px;
}
.scd-field input,
.scd-field select {
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
}
.scd-group {
  flex: 1 1 320px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
}
.scd-group h4 {
  margin: 0 0 8px;
}
.scd-check {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  flex-basis: 100%;
}
.scd-result,
.scd-versions,
.scd-logs {
  margin-top: 16px;
}
.scd-range {
  margin: 8px 0;
  font-size: 13px;
}
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 12px;
}
.tag-ok {
  background: #dcfce7;
  color: #166534;
}
.tag-fail {
  background: #fee2e2;
  color: #991b1b;
}
.tag-idle {
  background: #e2e8f0;
  color: #334155;
}
</style>
