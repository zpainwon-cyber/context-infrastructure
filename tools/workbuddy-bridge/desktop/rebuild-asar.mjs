// 用新 dist 重建 app.asar（保留原 packed 文件、按原规则 dist/** 全 unpacked）
import fs from 'node:fs'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { createRequire } from 'node:module'

const R = '/Users/zpain/.hermes/hermes-agent-v0.22.5-backup'
const RES = path.join(R, 'apps/desktop/release/mac-arm64/Hermes.app/Contents/Resources')
const A = path.join(RES, 'app.asar')
const UNPACKED = path.join(RES, 'app.asar.unpacked')
const NEWDIST = path.join(R, 'apps/desktop/dist')
const WORK = '/Users/zpain/.hermes/profiles/u7814-u53d1/cache/scratch/asar-work'
const SRC = path.join(WORK, 'src')
const OUT = path.join(WORK, 'out.asar')

const require = createRequire(path.join(R, 'package.json'))
const asar = require('@electron/asar')

fs.rmSync(WORK, { recursive: true, force: true })
fs.mkdirSync(SRC, { recursive: true })

// 1) 88 个 packed 文件：从原 asar 逐个读出来
const header = asar.getRawHeader(A).header
const packed = []
function walk(node, prefix) {
  for (const [name, v] of Object.entries(node.files || {})) {
    const p = prefix + '/' + name
    if (v.files) walk(v, p)
    else if (!v.unpacked) packed.push(p)
  }
}
walk(header, '')
for (const p of packed) {
  const rel = p.slice(1)
  const buf = asar.extractFile(A, rel)
  const dest = path.join(SRC, rel)
  fs.mkdirSync(path.dirname(dest), { recursive: true })
  fs.writeFileSync(dest, buf)
}
console.log('packed 文件还原:', packed.length)

// 2) 所有 unpacked 文件：从磁盘拷过来
execFileSync('cp', ['-R', UNPACKED + '/.', SRC + '/'])
console.log('unpacked 文件已拷贝')

// 3) 用新 build 的 dist 整体替换
fs.rmSync(path.join(SRC, 'dist'), { recursive: true, force: true })
execFileSync('cp', ['-R', NEWDIST, path.join(SRC, 'dist')])
const newFiles = execFileSync('find', [path.join(SRC, 'dist'), '-type', 'f'], { encoding: 'utf8' }).trim().split('\n').filter(Boolean)
console.log('新 dist 文件数:', newFiles.length)

// 4) 按原 electron-builder 规则重打包
await asar.createPackageWithOptions(SRC, OUT, { unpack: '**/dist/**', unpackDir: 'dist', dot: true })
console.log('新 asar 生成:', fs.statSync(OUT).size, 'bytes')

// 5) 验证
const list = asar.listPackage(OUT)
const html = fs.readFileSync(path.join(SRC, 'dist', 'index.html'), 'utf8')
const ref = (html.match(/assets\/index-[^"']+\.js/) || [])[0]
const hasRef = list.includes('/dist/' + ref)
const outUnpacked = OUT + '.unpacked'
const bundlePath = path.join(outUnpacked, 'dist', ref || '')
const hasWB3 = fs.existsSync(bundlePath) && fs.readFileSync(bundlePath, 'utf8').includes('workbuddy-3')
console.log('unpacked 产物存在:', fs.existsSync(outUnpacked), ' 文件数:', fs.existsSync(outUnpacked) ? execFileSync('find', [outUnpacked, '-type', 'f'], { encoding: 'utf8' }).trim().split('\n').filter(Boolean).length : 0)
console.log('bundle 路径:', bundlePath, '存在:', fs.existsSync(bundlePath))
let unpackedN = 0, packedN = 0
function count(n) { for (const v of Object.values(n.files || {})) { if (v.files) count(v); else if (v.unpacked) unpackedN++; else packedN++ } }
count(asar.getRawHeader(OUT).header)
console.log('新 asar 条目:', list.length, 'unpacked:', unpackedN, 'packed:', packedN)
console.log('index.html 引用:', ref, '在 header 中:', hasRef)
console.log('bundle 含 workbuddy-3:', hasWB3)
console.log('产物目录:', WORK)
