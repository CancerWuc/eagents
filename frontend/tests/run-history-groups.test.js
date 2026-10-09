import test from 'node:test'
import assert from 'node:assert/strict'
import { h } from 'vue'
import * as format from '../src/format.js'
import * as display from '../src/tool-display.js'
import { loadComponent, mount, findAll, textOf, click } from './helpers/vue-render.js'

const component = loadComponent(new URL('../src/components/RunHistoryGroups.vue', import.meta.url), {
  '../format.js': format, '../tool-display.js': display,
})
const first = { q: '第一轮', r: { runId: 'run-1', status: 'FAILED', errorCode: 'TOOL_ERROR', asUser: 'alice', inputRequired: true, routedServices: ['custom', 'custom'], elapsedMs: null, startedAt: null } }
const second = { q: '第二轮', r: { runId: 'run-2', status: 'SUCCEEDED', asUser: 'bob', routedServices: [], elapsedMs: 0, startedAt: '2026-10-08T12:03:04.123' } }
const groups = [{ contextId: 'testrun-a', latestAt: null, turns: [first, second] }]
const rows = root => findAll(root, node => node.tag === 'tr')
const cells = row => row.children.filter(node => node.tag === 'td')

test('history renders existing columns, status, empty times and group actions', () => {
  const view = mount(component, { groups, selectedRun: first.r, toolLabels: { custom: '自定义工具' } }, {
    'group-actions': ({ group }) => h('button', {}, `继续 ${group.contextId}`),
  })
  const tableRows = rows(view.root)
  assert.equal(tableRows.length, 2)
  assert.equal(cells(tableRows[0]).length, 6)
  assert.equal(textOf(cells(tableRows[0])[2]), '失败追问')
  assert.equal(textOf(cells(tableRows[0])[3]), '自定义工具（第 1 次） → 自定义工具（第 2 次）')
  assert.equal(textOf(cells(tableRows[0])[4]), '—')
  assert.equal(textOf(cells(tableRows[0])[5]), '刚刚')
  assert.equal(textOf(cells(tableRows[1])[4]), '0 ms')
  assert.equal(textOf(cells(tableRows[1])[5]), '12:03:04')
  assert.match(tableRows[0].props.class, /bg-canvas/)
  assert.doesNotMatch(tableRows[1].props.class, /bg-canvas/)
  assert.match(textOf(view.root), /2 轮对话testrun-a刚刚继续 testrun-a/)
  assert.equal(findAll(view.root, node => node.props.class === 'overflow-x-auto').length, 0)
  view.unmount()
})

test('monitoring retains extra columns, source labels and scroll container', () => {
  const view = mount(component, { groups, mode: 'runs' })
  const firstCells = cells(rows(view.root)[0])
  assert.equal(firstCells.length, 9)
  assert.equal(textOf(firstCells[2]), '失败')
  assert.equal(textOf(firstCells[3]), 'TOOL_ERROR')
  assert.equal(textOf(firstCells[5]), 'alice')
  assert.equal(textOf(firstCells[7]), '')
  assert.equal(textOf(firstCells[8]), '')
  assert.match(textOf(view.root), /^试运行2 轮对话/)
  assert.equal(findAll(view.root, node => node.props.class === 'overflow-x-auto').length, 1)
  view.unmount()
  for (const [contextId, source] of [['perf-1', '性能测试'], ['external-1', 'A2A']]) {
    const other = mount(component, { groups: [{...groups[0], contextId}], mode: 'runs' })
    assert.ok(textOf(other.root).startsWith(source))
    other.unmount()
  }
})

test('row selection emits original entries and action-cell clicks do not select rows', () => {
  const selected = [], added = []
  const view = mount(component, { groups, mode: 'runs', onSelect: entry => selected.push(entry) }, {
    'row-actions': ({ entry }) => h('button', { onClick: () => added.push(entry.r.runId) }, '加入'),
  })
  click(cells(rows(view.root)[0])[1])
  click(cells(rows(view.root)[0])[1])
  assert.deepEqual(selected, [first, first])
  click(findAll(rows(view.root)[1], node => node.tag === 'button')[0])
  assert.deepEqual(added, ['run-2'])
  assert.deepEqual(selected, [first, first])
  click(cells(rows(view.root)[0])[8])
  assert.equal(selected.length, 2)
  view.unmount()
})

test('empty groups and rows without a run id do not gain a selected state', () => {
  const empty = mount(component, { groups: [] })
  assert.equal(rows(empty.root).length, 0)
  empty.unmount()
  const view = mount(component, { groups: [{ contextId: '(无上下文)', turns: [{ q: '', r: { status: 'UNKNOWN' } }] }] })
  assert.doesNotMatch(rows(view.root)[0].props.class, /bg-canvas/)
  assert.equal(textOf(cells(rows(view.root)[0])[2]), 'UNKNOWN')
  view.unmount()
})
