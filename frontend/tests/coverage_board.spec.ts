import { expect, test } from '@playwright/test'

/**
 * 切片覆盖看板全链路验收：
 * 登记两条共 350 行的可用切片 -> 打开看板与同批清单 -> 对账中把 50 行切片标记过期
 * -> 对账总览保留钉选版本 -> 新一次查看采用更正版 300 行 -> 多次查看命中缓存不扫描。
 *
 * 通过 API 准备带唯一前缀的数据，页面断言只盯这批数据，避免受种子数据影响。
 */
const PREFIX = 'PW-COV'

async function activateSlice(
  request: import('@playwright/test').APIRequestContext,
  code: string,
  source: string,
  rows: string,
): Promise<number> {
  const created = await request.post('/api/segmentation', {
    data: { 切片编号: code, 源数据集: source, 切分维度: 'dt', 行数统计: rows },
  })
  expect(created.ok()).toBeTruthy()
  const payload = await created.json()
  const id = payload.entry.id
  for (const action of ['启动切分', '标记可用']) {
    const resp = await request.post(`/api/segmentation/${id}/actions`, { data: { action } })
    expect(resp.ok()).toBeTruthy()
  }
  return id
}

test('覆盖看板：版本钉选、到期更正与缓存复用', async ({ page, request }) => {
  test.setTimeout(120_000)
  const browserErrors: string[] = []
  page.on('pageerror', (e) => browserErrors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) browserErrors.push(`${r.status()} ${r.url()}`)
  })

  // 1. 登记 300 + 50 两条可用切片（源数据集名带唯一前缀，便于页面精确定位）
  const source300 = `${PREFIX}-order`
  const source50 = `${PREFIX}-pv`
  const id50 = await activateSlice(request, `${PREFIX}-300`, source300, '300')
  await activateSlice(request, `${PREFIX}-50`, source50, '50')

  await page.goto('/coverage_board')
  await expect(page.getByTestId('source-coverage-table')).toBeVisible()

  // 各源数据集可用行数在同一视图展示
  const row300 = page.locator('tbody tr', { hasText: source300 })
  const row50 = page.locator('tbody tr', { hasText: source50 })
  await expect(row300).toContainText('300')
  await expect(row50).toContainText('50')

  // 2. 打开同批清单核对：两条切片均纳入
  await page.getByTestId('open-manifest').click()
  await expect(page.getByTestId(`manifest-row-`).first()).toBeVisible()
  const manifestTable = page.getByTestId('manifest-slices-table')
  await expect(manifestTable).toContainText(`${PREFIX}-300`)
  await expect(manifestTable).toContainText(`${PREFIX}-50`)
  // 清单小计与总览同版同口径
  const versionBefore = await page.locator('.stat-value').first().innerText()
  expect(versionBefore.startsWith('v')).toBeTruthy()

  // 3. 多次刷新：第二次起命中物化缓存，扫描次数不增长
  await page.getByTestId('refresh-dashboard').click()
  await expect(page.getByTestId('version-meta')).toContainText('命中物化缓存')
  const scanText1 = await page.getByTestId('version-meta').innerText()
  await page.getByTestId('refresh-dashboard').click()
  const scanText2 = await page.getByTestId('version-meta').innerText()
  expect(scanText1).toContain('登记累计扫描次数')
  expect(scanText1).toEqual(scanText2)

  // 4. 打开对账会话，钉选当前版本
  await page.getByTestId('open-reconciliation').click()
  await expect(page.getByTestId('reconciliation-table')).toBeVisible()
  await expect(page.getByTestId('reconciliation-table')).toContainText('钉选版本')

  // 5. 对账中收到到期更正：50 行切片标记过期
  const expireResp = await request.post(`/api/segmentation/${id50}/actions`, {
    data: { action: '标记过期' },
  })
  expect(expireResp.ok()).toBeTruthy()

  // 对账总览仍采用钉选版本，50 行来源仍在
  await page.getByRole('button', { name: '查看对账总览', exact: true }).click()
  await expect(page.getByTestId('reconciliation-total')).toBeVisible()
  await expect(page.getByTestId('reconciliation-table')).toContainText(
    '登记已有到期更正，会话仍保留钉选版本的完整营业日报数',
  )

  // 6. 新一次查看：立即采用更正版，50 行来源消失、版本号 +1
  await page.getByTestId('refresh-dashboard').click()
  await expect(page.locator('tbody tr', { hasText: source50 })).toHaveCount(0)
  await expect(page.locator('tbody tr', { hasText: source300 })).toContainText('300')
  const versionAfter = await page.locator('.stat-value').first().innerText()
  expect(versionAfter).not.toEqual(versionBefore)

  // 版本历史可追溯旧版（旧版仍含 50 行来源）
  await expect(page.getByTestId('manifest-history-table')).toContainText(versionBefore.slice(1))

  expect(browserErrors).toEqual([])
})
