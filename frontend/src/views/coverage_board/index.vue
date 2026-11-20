<template>
  <section class="page" data-module="coverage-board">
    <header class="page-head">
      <div>
        <h2>切片覆盖看板</h2>
        <p class="page-desc">
          关联数据分层维护的可用切片清单汇总覆盖量；只把「可用且行数合法」的切片计入可用量，
          已过期与待切分数据单列。清单按版本固化，对账期间钉选版本，可追溯每个源数据集的计数来源。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="refresh-dashboard" type="button" @click="reloadDashboard">
          刷新看板
        </button>
        <button class="btn" data-testid="publish-manifest" type="button" @click="publishManifest">
          发布新版清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">清单版本</span>
        <strong class="stat-value">v{{ dashboard?.manifest_version ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">可用行数合计</span>
        <strong class="stat-value" data-testid="total-available-rows">
          {{ dashboard?.total_available_rows ?? '—' }}
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">纳入切片数</span>
        <strong class="stat-value">{{ dashboard?.included_slice_count ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已过期 / 待切分 / 行数非法</span>
        <strong class="stat-value">
          {{ dashboard ? `${dashboard.excluded_summary['已过期']} / ${dashboard.excluded_summary['待切分']} / ${dashboard.excluded_summary['行数非法']}` : '—' }}
        </strong>
      </article>
    </div>

    <div class="version-bar" data-testid="version-meta">
      <span>发布时间：{{ dashboard?.published_at ?? '—' }}</span>
      <span>覆盖口径：{{ dashboard?.coverage_rule ?? '—' }}</span>
      <span :class="dashboard?.served_from_cache ? 'cache-hit' : 'cache-miss'">
        {{ dashboard?.served_from_cache ? '命中物化缓存（未扫描登记）' : '本次发布新版本（全量扫描一次）' }}
      </span>
      <span>登记累计扫描次数：{{ dashboard?.registry_scan_count ?? '—' }}</span>
      <span v-if="dashboard?.remark">备注：{{ dashboard.remark }}</span>
    </div>

    <h3>各源数据集可用行数</h3>
    <table class="data-table" data-testid="source-coverage-table">
      <thead>
        <tr><th>源数据集</th><th>可用行数</th><th>切片数</th><th>计数来源</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in dashboard?.source_coverage ?? []" :key="row.源数据集">
          <td>{{ row.源数据集 }}</td>
          <td>{{ row.可用行数 }}</td>
          <td>{{ row.切片数 }}</td>
          <td>
            <span v-for="(src, idx) in row.来源切片" :key="src.entry_id" class="source-chip">
              {{ src.切片编号 }}（{{ src.row_count }} 行）<span v-if="idx < row.来源切片.length - 1">、</span>
            </span>
          </td>
        </tr>
        <tr v-if="!dashboard?.source_coverage?.length">
          <td colspan="4" class="empty-state">暂无可用切片，先到数据分层登记并标记可用</td>
        </tr>
      </tbody>
      <tfoot>
        <tr>
          <td>小计（与总览同版同口径）</td>
          <td data-testid="source-subtotal">{{ sourceSubtotal }}</td>
          <td colspan="2"></td>
        </tr>
      </tfoot>
    </table>

    <div class="section-head">
      <h3>同批清单核对</h3>
      <div class="page-actions">
        <button class="btn" data-testid="open-manifest" type="button" @click="openCurrentManifest">
          打开当前版清单
        </button>
      </div>
    </div>
    <table class="data-table" data-testid="manifest-slices-table">
      <thead>
        <tr><th>切片编号</th><th>源数据集</th><th>状态</th><th>行数</th><th>处理结论</th></tr>
      </thead>
      <tbody>
        <template v-for="item in manifestDetail" :key="`${item.entry_id}-${item.status}`">
          <tr :data-testid="`manifest-row-${item.entry_id}`">
            <td>{{ item.切片编号 }}</td>
            <td>{{ item.源数据集 }}</td>
            <td>{{ item.status }}</td>
            <td>{{ item.row_count ?? '—' }}</td>
            <td>{{ item.排除原因 ?? '纳入可用量' }}</td>
          </tr>
        </template>
        <tr v-if="!manifestDetail.length">
          <td colspan="5" class="empty-state">点击「打开当前版清单」加载同批明细</td>
        </tr>
      </tbody>
    </table>

    <div class="section-head">
      <h3>清单版本历史（对账追溯）</h3>
    </div>
    <table class="data-table" data-testid="manifest-history-table">
      <thead>
        <tr><th>版本</th><th>发布时间</th><th>可用行数</th><th>纳入/排除切片</th><th>备注</th><th>操作</th></tr>
      </thead>
      <tbody>
        <tr v-for="item in manifestHistory" :key="item.manifest_version">
          <td>v{{ item.manifest_version }}</td>
          <td>{{ item.published_at }}</td>
          <td>{{ item.total_available_rows }}</td>
          <td>{{ item.included_slice_count }} / {{ item.excluded_slice_count }}</td>
          <td>{{ item.remark ?? '—' }}</td>
          <td><button class="link" type="button" @click="openVersion(item.manifest_version)">追溯该版</button></td>
        </tr>
        <tr v-if="!manifestHistory.length">
          <td colspan="6" class="empty-state">暂无历史版本</td>
        </tr>
      </tbody>
    </table>

    <div class="section-head">
      <h3>对账会话（版本钉选）</h3>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-reconciliation" type="button" @click="openReconciliation">
          打开对账（钉选当前版）
        </button>
        <button
          class="btn"
          type="button"
          :disabled="!session"
          @click="loadSessionDashboard"
        >
          查看对账总览
        </button>
        <button class="btn ghost" type="button" :disabled="!session" @click="closeReconciliation">
          结束对账
        </button>
      </div>
    </div>
    <table v-if="session" class="data-table" data-testid="reconciliation-table">
      <tbody>
        <tr><td>会话编号</td><td>{{ session.session_id }}</td></tr>
        <tr><td>业务单号</td><td>{{ session.business_ref ?? '—' }}</td></tr>
        <tr><td>钉选版本</td><td>v{{ session.pinned_version }}</td></tr>
        <tr><td>最新已发布版本</td><td>v{{ session.latest_version }}</td></tr>
        <tr>
          <td>版本状态</td>
          <td :class="session.version_stale ? 'cache-miss' : 'cache-hit'">
            {{ session.version_stale ? '登记已有到期更正，会话仍保留钉选版本的完整营业日报数' : '钉选版本即最新' }}
          </td>
        </tr>
        <tr v-if="sessionDashboard">
          <td>对账总览可用量</td>
          <td data-testid="reconciliation-total">{{ sessionDashboard.total_available_rows }} 行（v{{ sessionDashboard.manifest_version }}）</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type SourceCoverage = {
  源数据集: string
  可用行数: number
  切片数: number
  来源切片: { entry_id: number; 切片编号: string; row_count: number | null }[]
}

type Dashboard = {
  manifest_version: number
  published_at: string
  remark: string | null
  coverage_rule: string
  total_available_rows: number
  included_slice_count: number
  source_coverage: SourceCoverage[]
  excluded_summary: Record<string, number>
  excluded_slices: Record<string, unknown>[]
  served_from_cache: boolean
  registry_scan_count: number
}

type ManifestSlice = {
  entry_id: number
  切片编号: string
  源数据集: string
  status: string
  row_count: number | null
  排除原因?: string
}

type Manifest = {
  manifest_version: number
  included_slices: ManifestSlice[]
  excluded_slices: ManifestSlice[]
}

type ManifestHistoryItem = {
  manifest_version: number
  published_at: string
  remark: string | null
  registry_watermark: number
  total_available_rows: number
  included_slice_count: number
  excluded_slice_count: number
}

type SessionInfo = {
  session_id: string
  business_ref: string | null
  pinned_version: number
  latest_version: number
  version_stale: boolean
  closed_at: string | null
}

const dashboard = ref<Dashboard | null>(null)
const manifest = ref<Manifest | null>(null)
const manifestHistory = ref<ManifestHistoryItem[]>([])
const session = ref<SessionInfo | null>(null)
const sessionDashboard = ref<Dashboard | null>(null)
const errorMessage = ref('')

const manifestDetail = computed<ManifestSlice[]>(() => {
  if (!manifest.value) return []
  return [...manifest.value.included_slices, ...manifest.value.excluded_slices]
})

const sourceSubtotal = computed(() =>
  (dashboard.value?.source_coverage ?? []).reduce((sum, item) => sum + item.可用行数, 0))

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

async function postJson<T>(path: string): Promise<T> {
  const response = await request(path, { method: 'POST' })
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

async function reloadDashboard() {
  errorMessage.value = ''
  try {
    dashboard.value = await getJson<Dashboard>('/api/coverage-board/dashboard')
    await loadHistory()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '看板读取失败'
  }
}

async function loadHistory() {
  const payload = await getJson<{ items: ManifestHistoryItem[] }>('/api/coverage-board/manifests')
  manifestHistory.value = payload.items
}

async function openCurrentManifest() {
  if (!dashboard.value) return
  await openVersion(dashboard.value.manifest_version)
}

async function openVersion(version: number) {
  errorMessage.value = ''
  try {
    manifest.value = await getJson<Manifest>(`/api/coverage-board/manifests/${version}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清单读取失败'
  }
}

async function publishManifest() {
  errorMessage.value = ''
  try {
    await postJson<unknown>('/api/coverage-board/manifests/publish')
    await reloadDashboard()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清单发布失败'
  }
}

async function openReconciliation() {
  errorMessage.value = ''
  try {
    const payload = await postJson<{ ok: boolean; entry: SessionInfo }>(
      '/api/coverage-board/reconciliations?business_ref=DAILY-20261120')
    session.value = payload.entry
    sessionDashboard.value = null
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账打开失败'
  }
}

async function loadSessionDashboard() {
  if (!session.value) return
  errorMessage.value = ''
  try {
    sessionDashboard.value = await getJson<Dashboard>(
      `/api/coverage-board/reconciliations/${session.value.session_id}/dashboard`)
    session.value = await getJson<SessionInfo>(
      `/api/coverage-board/reconciliations/${session.value.session_id}`)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账总览读取失败'
  }
}

async function closeReconciliation() {
  if (!session.value) return
  errorMessage.value = ''
  try {
    const payload = await postJson<{ entry: SessionInfo }>(
      `/api/coverage-board/reconciliations/${session.value.session_id}/close`)
    session.value = payload.entry
    await reloadDashboard()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结束对账失败'
  }
}

onMounted(reloadDashboard)
</script>

<style scoped>
.version-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 14px;
  font-size: 13px;
}
.cache-hit { color: #067647; font-weight: 600; }
.cache-miss { color: #b54708; font-weight: 600; }
.section-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 18px 0 8px;
}
.source-chip { white-space: nowrap; }
tfoot td { font-weight: 600; background: #f8fafc; }
</style>
