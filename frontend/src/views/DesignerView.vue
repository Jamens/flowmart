<template>
  <div>
    <div class="toolbar">
      <el-select v-model="defId" placeholder="选择流程" style="width: 240px" @change="loadGraph">
        <el-option v-for="d in definitions" :key="d.id" :label="`${d.name} (${d.code}) v${d.version}`" :value="d.id" />
      </el-select>
      <el-button @click="loadDefinitions">刷新列表</el-button>
      <el-button @click="addNode">新增节点</el-button>
      <el-button @click="addTransVisible = true">新增流转</el-button>
      <el-button type="primary" @click="save" :disabled="!graph">保存</el-button>
      <el-button type="success" @click="publish" :disabled="!graph">发布</el-button>
      <el-tag :type="graph?.status === 'published' ? 'success' : 'info'">
        {{ graph?.status === 'published' ? '已发布' : '草稿' }}
      </el-tag>
    </div>

    <div class="designer">
      <!-- 画布 -->
      <div class="canvas-wrap">
        <svg
          ref="svgRef"
          class="canvas"
          @mousemove="onMove"
          @mouseup="onUp"
          @mouseleave="onUp"
        >
          <defs>
            <marker id="arrow" markerWidth="9" markerHeight="9" refX="8" refY="3" orient="auto">
              <path d="M0,0 L0,6 L8,3 z" fill="#58a6ff" />
            </marker>
          </defs>

          <!-- 连线 -->
          <g>
            <path
              v-for="(t, i) in graph?.transitions || []"
              :key="'e' + i"
              :class="['edge', { active: selectedTrans === t }]"
              :d="edgePath(t)"
              marker-end="url(#arrow)"
            />
            <text
              v-for="(t, i) in graph?.transitions || []"
              :key="'l' + i"
              class="edge-label"
              :x="edgeLabel(t).x"
              :y="edgeLabel(t).y"
              text-anchor="middle"
              @click="selectedTrans = t"
            >
              {{ t.event }}{{ t.condition_expr ? ` [${t.condition_expr}]` : '' }}
            </text>
          </g>

          <!-- 节点 -->
          <g
            v-for="n in graph?.nodes || []"
            :key="n.key"
            :class="['node', n.node_type, { selected: selected === n }]"
            @mousedown="onNodeDown($event, n)"
          >
            <rect :x="n.x - NODE_W / 2" :y="n.y - NODE_H / 2" :width="NODE_W" :height="NODE_H" rx="8" />
            <text :x="n.x" :y="n.y - 3" text-anchor="middle" class="node-name">{{ n.name }}</text>
            <text :x="n.x" :y="n.y + 15" text-anchor="middle" class="node-key">{{ n.key }}</text>
          </g>
        </svg>
        <p class="hint">拖拽移动节点 · 点击选中后在右侧编辑属性 · 坐标会随流程一起保存</p>
      </div>

      <!-- 属性面板 -->
      <div class="panel">
        <template v-if="selected">
          <h4>节点属性</h4>
          <el-form label-width="70px" size="small">
            <el-form-item label="标识 key">
              <el-input v-model="selected.key" />
            </el-form-item>
            <el-form-item label="名称">
              <el-input v-model="selected.name" />
            </el-form-item>
            <el-form-item label="类型">
              <el-select v-model="selected.node_type">
                <el-option label="开始" value="start" />
                <el-option label="任务" value="task" />
                <el-option label="结束" value="end" />
              </el-select>
            </el-form-item>
            <el-form-item label="坐标">
              <el-input-number v-model="selected.x" :step="10" /> ·
              <el-input-number v-model="selected.y" :step="10" />
            </el-form-item>
          </el-form>
          <el-button type="danger" size="small" plain @click="removeNode">删除节点</el-button>
        </template>

        <template v-else-if="selectedTrans">
          <h4>流转属性</h4>
          <el-form label-width="70px" size="small">
            <el-form-item label="从">
              <el-select v-model="selectedTrans.from">
                <el-option v-for="n in graph.nodes" :key="n.key" :label="n.name" :value="n.key" />
              </el-select>
            </el-form-item>
            <el-form-item label="到">
              <el-select v-model="selectedTrans.to">
                <el-option v-for="n in graph.nodes" :key="n.key" :label="n.name" :value="n.key" />
              </el-select>
            </el-form-item>
            <el-form-item label="事件">
              <el-input v-model="selectedTrans.event" />
            </el-form-item>
            <el-form-item label="条件">
              <el-input v-model="selectedTrans.condition_expr" placeholder="如 amount >= 1000，留空表示无条件" />
            </el-form-item>
            <el-form-item label="优先级">
              <el-input-number v-model="selectedTrans.priority" :min="0" />
            </el-form-item>
            <el-form-item label="说明">
              <el-input v-model="selectedTrans.description" />
            </el-form-item>
          </el-form>
          <el-button type="danger" size="small" plain @click="removeTrans">删除流转</el-button>
        </template>

        <template v-else>
          <el-empty description="选中一个节点或流转以编辑" :image-size="60" />
          <div class="summary">
            <p>当前流程：<b>{{ graph?.nodes.length || 0 }}</b> 个节点，<b>{{ graph?.transitions.length || 0 }}</b> 条流转</p>
            <p class="tip">
              同一「起点 + 事件」可配置多条流转，用<b>条件表达式</b>分流，
              优先级数值小的先匹配。例如退款按金额走不同分支。
            </p>
          </div>
        </template>
      </div>
    </div>

    <!-- 新增流转 -->
    <el-dialog v-model="addTransVisible" title="新增流转" width="420px">
      <el-form label-width="80px">
        <el-form-item label="起点">
          <el-select v-model="newTrans.from" style="width: 100%">
            <el-option v-for="n in graph?.nodes || []" :key="n.key" :label="n.name" :value="n.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="终点">
          <el-select v-model="newTrans.to" style="width: 100%">
            <el-option v-for="n in graph?.nodes || []" :key="n.key" :label="n.name" :value="n.key" />
          </el-select>
        </el-form-item>
        <el-form-item label="事件">
          <el-input v-model="newTrans.event" placeholder="如 pay / ship / approve" />
        </el-form-item>
        <el-form-item label="条件">
          <el-input v-model="newTrans.condition_expr" placeholder="可选，如 amount >= 1000" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addTransVisible = false">取消</el-button>
        <el-button type="primary" @click="confirmAddTrans">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, reactive } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const NODE_W = 150
const NODE_H = 52

const definitions = ref([])
const defId = ref(null)
const graph = ref(null)
const selected = ref(null)
const selectedTrans = ref(null)
const svgRef = ref(null)
const dragging = ref(null)
const dragOffset = reactive({ x: 0, y: 0 })
const addTransVisible = ref(false)
const newTrans = reactive({ from: '', to: '', event: '', condition_expr: '' })

function nodeCenter(key) {
  return graph.value?.nodes.find((n) => n.key === key)
}

/** 三次贝塞尔连线：按两节点相对位置选择左右出线，避免连线穿过节点 */
function edgePath(t) {
  const a = nodeCenter(t.from)
  const b = nodeCenter(t.to)
  if (!a || !b) return ''
  const forward = b.x >= a.x
  const x1 = forward ? a.x + NODE_W / 2 : a.x - NODE_W / 2
  const x2 = forward ? b.x - NODE_W / 2 : b.x + NODE_W / 2
  const dx = Math.abs(x2 - x1) * 0.5 + 30
  const c1 = forward ? x1 + dx : x1 - dx
  const c2 = forward ? x2 - dx : x2 + dx
  return `M${x1},${a.y} C${c1},${a.y} ${c2},${b.y} ${x2},${b.y}`
}

function edgeLabel(t) {
  const a = nodeCenter(t.from)
  const b = nodeCenter(t.to)
  if (!a || !b) return { x: 0, y: 0 }
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 - 6 }
}

// ---------- 拖拽：屏幕坐标 -> SVG 坐标 ----------
function toSvgPoint(e) {
  const rect = svgRef.value.getBoundingClientRect()
  return { x: e.clientX - rect.left, y: e.clientY - rect.top }
}

function onNodeDown(e, node) {
  selected.value = node
  selectedTrans.value = null
  dragging.value = node
  const p = toSvgPoint(e)
  dragOffset.x = p.x - node.x
  dragOffset.y = p.y - node.y
}

function onMove(e) {
  if (!dragging.value) return
  const p = toSvgPoint(e)
  dragging.value.x = Math.round(p.x - dragOffset.x)
  dragging.value.y = Math.round(p.y - dragOffset.y)
}

function onUp() {
  dragging.value = null
}

// ---------- 数据操作 ----------
async function loadDefinitions() {
  definitions.value = await api.listDefinitions()
  if (!defId.value && definitions.value.length) {
    defId.value = definitions.value[0].id
    await loadGraph()
  }
}

async function loadGraph() {
  if (!defId.value) return
  graph.value = await api.getDefinition(defId.value)
  selected.value = null
  selectedTrans.value = null
}

function addNode() {
  if (!graph.value) return
  const key = `node_${Date.now().toString(36)}`
  graph.value.nodes.push({
    key,
    name: '新节点',
    node_type: 'task',
    x: 200 + graph.value.nodes.length * 20,
    y: 320,
  })
  selected.value = graph.value.nodes[graph.value.nodes.length - 1]
  selectedTrans.value = null
}

function removeNode() {
  const n = selected.value
  graph.value.nodes = graph.value.nodes.filter((x) => x !== n)
  // 同时清掉引用了它的流转，否则保存后会出现悬空引用
  graph.value.transitions = graph.value.transitions.filter((t) => t.from !== n.key && t.to !== n.key)
  selected.value = null
}

function removeTrans() {
  graph.value.transitions = graph.value.transitions.filter((t) => t !== selectedTrans.value)
  selectedTrans.value = null
}

function confirmAddTrans() {
  if (!newTrans.from || !newTrans.to || !newTrans.event) {
    return ElMessage.warning('起点、终点、事件均不能为空')
  }
  graph.value.transitions.push({
    from: newTrans.from,
    to: newTrans.to,
    event: newTrans.event,
    condition_expr: newTrans.condition_expr || '',
    priority: 100,
    description: '',
  })
  Object.assign(newTrans, { from: '', to: '', event: '', condition_expr: '' })
  addTransVisible.value = false
}

async function save() {
  try {
    await api.updateDefinition(defId.value, {
      code: graph.value.code,
      name: graph.value.name,
      description: graph.value.description,
      nodes: graph.value.nodes.map((n) => ({
        key: n.key,
        name: n.name,
        node_type: n.node_type,
        x: n.x,
        y: n.y,
        meta: {},
      })),
      transitions: graph.value.transitions.map((t) => ({
        from_node_key: t.from,
        to_node_key: t.to,
        event: t.event,
        condition_expr: t.condition_expr,
        priority: t.priority,
        description: t.description,
      })),
    })
    ElMessage.success('保存成功')
    await loadGraph()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function publish() {
  try {
    await api.publishDefinition(defId.value)
    ElMessage.success('发布成功，新订单将按此流程流转')
    await loadGraph()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(loadDefinitions)
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 10px;
  align-items: center;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.designer {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.canvas-wrap {
  flex: 1;
  min-width: 0;
}
.canvas {
  width: 100%;
  height: 520px;
  background: #161b22;
  border: 1px solid #2d3542;
  border-radius: 8px;
  user-select: none;
}
.node rect {
  fill: #1c2230;
  stroke: #2d3542;
  stroke-width: 1.5;
  cursor: move;
}
.node.start rect {
  stroke: #3fb950;
}
.node.end rect {
  stroke: #f85149;
}
.node.selected rect {
  stroke: #58a6ff;
  stroke-width: 2.5;
}
.node-name {
  fill: #e6edf3;
  font-size: 13px;
  font-weight: 600;
  pointer-events: none;
}
.node-key {
  fill: #8b949e;
  font-size: 10.5px;
  pointer-events: none;
}
.edge {
  stroke: #3d4552;
  stroke-width: 1.6;
  fill: none;
}
.edge.active {
  stroke: #58a6ff;
  stroke-width: 2.5;
}
.edge-label {
  fill: #8b949e;
  font-size: 10.5px;
  cursor: pointer;
}
.edge-label:hover {
  fill: #58a6ff;
}
.panel {
  width: 330px;
  flex-shrink: 0;
  background: #161b22;
  border: 1px solid #2d3542;
  border-radius: 8px;
  padding: 14px;
}
h4 {
  margin: 0 0 12px;
  font-size: 14px;
}
.hint,
.tip {
  color: #8b949e;
  font-size: 12px;
  margin: 8px 0 0;
}
.summary {
  margin-top: 12px;
}
</style>
