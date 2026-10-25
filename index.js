/**
 * dsh-image-shim —— 把本地图像代理（image-shim.py）挂在 DSH 宿主生命周期上。
 *
 * 解决什么：
 *   出图链路依赖 127.0.0.1:3089 上那个去水印代理。原来靠两条 HKCU\...\Run 自启，
 *   但两条会互相抢端口（先起的占住 3089、后起的默默死掉），而且只在登录时触发一次 ——
 *   结果是「自启项都在，服务却没活」，出图直接失败。
 *   本插件把「保证它在跑」这件事收进宿主：DSH 起来它就在，掉了自动拉回来。
 *
 * 行为：
 *   1. apply 时先探活（GET /draw/status 要 200 且 ok=true，不是只探 TCP —— 进程卡死也能发现）；
 *      不健康就拉起 `pythonw image-shim.py`。
 *   2. 之后每 `probeIntervalSec` 秒探一次；连续 `failureThreshold` 次失败才动手，避免误杀。
 *   3. 拉起用 DETACHED_PROCESS，子进程不挂在 DSH 的作业对象里 —— DSH 重启/崩溃不会带走它，
 *      少一次无谓的重启等待。
 *   4. 端口被别的进程占着（不是我们拉的那个）时不乱杀，只记日志。
 *
 * 配置（见 cordis.patch.yml）：全部有默认值，不配也能跑。
 *
 * 为什么不把去水印逻辑搬进 Node：
 *   定位 + 镜像重建那套是 numpy/PIL 的活，搬过来要重写一遍算法且更难对齐实测结果；
 *   python 侧已经是验证过的实现，这层只负责「让它一直活着」。
 */
import { execFileSync, spawn } from 'node:child_process'
import { existsSync, readdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import http from 'node:http'
import net from 'node:net'

export const name = 'dsh-image-shim'

const HERE = dirname(fileURLToPath(import.meta.url))

const DEFAULTS = {
  enabled: true,
  pythonPath: 'auto',
  port: 3089,
  probeIntervalSec: 30,
  probeTimeoutMs: 6000,
  failureThreshold: 2,
  startupGraceSec: 8,
  stopOnDispose: false,
}

/**
 * 找 Python 解释器。
 *
 * 原来 pythonPath 写死成某个具体路径，换个机器 / 挪个目录就拉不起来，
 * 而且报错只说「拉不起来」，看不出是路径失效。现在默认 'auto'：按候选表逐个试，
 * 谁能 `import PIL, numpy` 就用谁。配置里写死路径仍然有效（作为第一候选），
 * 环境变量 DSH_IMAGE_SHIM_PYTHON 也能覆盖。
 */
const PY_EXE_NAMES = ['pythonw.exe', 'python.exe']
const NEEDED_MODULES = 'import PIL, numpy'

/** pythonw.exe 没有 stdout，没法跑 import 测试；探针一律用同目录的 python.exe。 */
function probeExeFor(exe) {
  if (/pythonw\.exe$/i.test(exe)) {
    const sibling = join(dirname(exe), 'python.exe')
    return existsSync(sibling) ? sibling : null
  }
  return exe
}

/** pythonw.exe 是 python.exe 的无控制台版；有就用它，避免弹黑框。 */
function windowlessOf(exe) {
  if (/pythonw\.exe$/i.test(exe)) return exe
  const sibling = join(dirname(exe), 'pythonw.exe')
  return existsSync(sibling) ? sibling : exe
}

/** 真跑一次 import 验证依赖齐不齐 —— 只看文件存在不够，缺 PIL 一样跑不起来。 */
function interpreterUsable(exe) {
  if (!exe || !existsSync(exe)) return false
  const probe = probeExeFor(exe)
  if (!probe) return false
  try {
    execFileSync(probe, ['-c', NEEDED_MODULES], { stdio: 'ignore', timeout: 8000, windowsHide: true })
    return true
  } catch {
    return false
  }
}

/** 按优先级列出候选解释器。显式配置 > 环境变量 > 官方安装目录 > 盘根 > py 启动器 > PATH。 */
function candidateInterpreters(cfg) {
  const out = []
  const push = (p) => {
    if (!p) return
    const v = String(p).trim()
    if (v && !out.includes(v)) out.push(v)
  }

  const explicit = String(cfg.pythonPath || '').trim()
  if (explicit && explicit.toLowerCase() !== 'auto') push(explicit)
  push(process.env.DSH_IMAGE_SHIM_PYTHON)

  const localAppData = process.env.LOCALAPPDATA
  if (localAppData) {
    const root = join(localAppData, 'Programs', 'Python')
    if (existsSync(root)) {
      try {
        for (const d of readdirSync(root).filter((n) => /^Python3/i.test(n)).sort().reverse()) {
          for (const n of PY_EXE_NAMES) push(join(root, d, n))
        }
      } catch {
        /* 目录读不了就算了 */
      }
    }
  }

  for (const drive of ['C:', 'D:', 'E:']) {
    try {
      for (const d of readdirSync(`${drive}\\`).filter((n) => /^Python3/i.test(n)).sort().reverse()) {
        for (const n of PY_EXE_NAMES) push(join(`${drive}\\`, d, n))
      }
    } catch {
      /* 盘不存在 / 无权限 */
    }
  }

  for (const dir of String(process.env.PATH || '').split(';')) {
    if (!dir.trim()) continue
    for (const n of PY_EXE_NAMES) push(join(dir.trim(), n))
  }

  return out
}

/** `py -3` 启动器指向的解释器（PATH 里没有 python 时的兜底）。 */
function pyLauncherExe() {
  try {
    const out = execFileSync('py', ['-3', '-c', 'import sys;print(sys.executable)'], {
      encoding: 'utf8',
      timeout: 8000,
      windowsHide: true,
    })
    const lines = String(out).trim().split(/\r?\n/)
    const exe = lines[lines.length - 1].trim()
    return exe && existsSync(exe) ? exe : null
  } catch {
    return null
  }
}

function resolveConfig(raw) {
  const cfg = { ...DEFAULTS }
  if (raw && typeof raw === 'object') {
    for (const key of Object.keys(DEFAULTS)) {
      const v = raw[key]
      if (v === undefined || v === null) continue
      if (typeof DEFAULTS[key] === 'number') {
        const n = Number(v)
        if (Number.isFinite(n) && n > 0) cfg[key] = n
      } else if (typeof DEFAULTS[key] === 'boolean') {
        cfg[key] = v === true || v === 'true'
      } else if (typeof DEFAULTS[key] === 'string' && String(v).trim()) {
        cfg[key] = String(v).trim()
      }
    }
  }
  return cfg
}

function log(ctx, message, level = 'info') {
  const logger = ctx?.logger
  const sink = logger ? Reflect.get(logger, level) : undefined
  if (typeof sink === 'function') sink.call(logger, `image-shim: ${message}`)
  else if (level === 'error') console.error(`image-shim: ${message}`)
  else console.log(`image-shim: ${message}`)
}

/** HTTP 探活：TCP 通但进程卡死（事件循环堵住）时返回 false。 */
function probe(port, timeoutMs = 4000) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: '127.0.0.1', port, path: '/draw/status', timeout: timeoutMs },
      (res) => {
        if (res.statusCode !== 200) {
          res.resume()
          resolve(false)
          return
        }
        let buf = ''
        res.setEncoding('utf8')
        res.on('data', (c) => {
          buf += c
          if (buf.length > 65536) req.destroy()
        })
        res.on('end', () => {
          try {
            resolve(JSON.parse(buf).ok === true)
          } catch {
            resolve(false)
          }
        })
      },
    )
    req.on('timeout', () => {
      req.destroy()
      resolve(false)
    })
    req.on('error', () => resolve(false))
  })
}
/** 端口是否有人在听：用来区分「还没起」和「别人（比如旧的 watchdog）占着」。 */
function portListening(port, timeoutMs = 1500) {
  return new Promise((resolve) => {
    const socket = new net.Socket()
    socket.setTimeout(timeoutMs)
    socket.once('connect', () => {
      socket.destroy()
      resolve(true)
    })
    socket.once('timeout', () => {
      socket.destroy()
      resolve(false)
    })
    socket.once('error', () => resolve(false))
    socket.connect(port, '127.0.0.1')
  })
}

/**
 * 找出占着 port 的 shim 进程并杀掉；只杀命令行里确实带 image-shim.py 的，
 * 避免误伤其它恰好用这个端口的程序。返回被杀的 pid，没杀返回 0。
 */
function killShimOnPort(port) {
  let out = ''
  try {
    out = execFileSync(
      'powershell.exe',
      [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `$c = Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; ` +
          `if ($c) { $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue; ` +
          `if ($p -and $p.CommandLine -match 'image-shim\\.py') { Write-Output $p.ProcessId } }`,
      ],
      { encoding: 'utf8', timeout: 8000, windowsHide: true },
    ).trim()
  } catch {
    return 0
  }
  const pid = Number(out.split(/\s+/)[0])
  if (!Number.isInteger(pid) || pid <= 0) return 0
  try {
    execFileSync('taskkill', ['/PID', String(pid), '/F'], { stdio: 'ignore', timeout: 8000, windowsHide: true })
    return pid
  } catch {
    return 0
  }
}
export function apply(ctx, rawConfig) {
  const cfg = resolveConfig(rawConfig)
  if (!cfg.enabled) {
    log(ctx, 'disabled by config')
    return
  }

  const script = join(HERE, 'image-shim.py')
  if (!existsSync(script)) {
    log(ctx, `script missing: ${script}`, 'error')
    return
  }

  // 解释器只在 apply 时解析一次（每次 spawn 都跑一遍 import 探针太慢）。
  // 解析失败不直接放弃 —— 也许用户过会儿才装好 Python，留给 launch 时重试。
  let pythonExe = null
  function ensurePython() {
    if (pythonExe) return pythonExe
    // 顺序：显式配置 / 环境变量 → py 启动器（系统注册的官方 Python）→ 各盘 Python3* 目录 → PATH。
    // py 启动器排在 PATH 前面，因为 PATH 里常混进别的软件自带的 Python（FreeCAD、某些 IDE），
    // 那些能用但不可靠（卸载那个软件就断），所以只当兜底。
    const list = candidateInterpreters(cfg)
    const fromLauncher = pyLauncherExe()
    const head = []
    const explicit = String(cfg.pythonPath || '').trim()
    if (explicit && explicit.toLowerCase() !== 'auto' && list.includes(explicit)) head.push(explicit)
    const envExe = String(process.env.DSH_IMAGE_SHIM_PYTHON || '').trim()
    if (envExe && list.includes(envExe) && !head.includes(envExe)) head.push(envExe)
    if (fromLauncher && !head.includes(fromLauncher)) head.push(fromLauncher)
    const candidates = [...head, ...list.filter((p) => !head.includes(p))]
    for (const exe of candidates) {
      if (interpreterUsable(exe)) {
        pythonExe = windowlessOf(exe)
        log(ctx, `python resolved: ${pythonExe}`)
        return pythonExe
      }
    }
    log(ctx, `no usable python found (need PIL + numpy). tried: ${candidates.join(' | ') || '(none)'}`, 'error')
    return null
  }
  ensurePython()

  let child = null
  let timer = null
  let stopped = false
  let failures = 0
  let spawning = false
  function launch(reason) {
    if (spawning || stopped) return
    spawning = true
    try {
      const env = { ...process.env, SHIM_PORT: String(cfg.port) }
      const exe = ensurePython()
      if (!exe) {
        spawning = false
        log(ctx, `cannot launch (${reason}): no usable python interpreter`, 'error')
        return
      }
      const p = spawn(exe, [script], {
        cwd: HERE,
        env,
        stdio: 'ignore',
        windowsHide: true,
        detached: true,
      })
      p.on('error', (error) => {
        spawning = false
        log(ctx, `spawn failed (${reason}): ${error.message}`, 'error')
      })
      p.on('exit', (code, signal) => {
        spawning = false
        if (child === p) child = null
        if (!stopped) log(ctx, `shim exited (code=${code} signal=${signal}) — will re-probe`, 'warn')
      })
      p.unref()
      child = p
      log(ctx, `launched (${reason}) pid=${p.pid} port=${cfg.port}`)
    } catch (error) {
      spawning = false
      log(ctx, `launch threw (${reason}): ${error?.message ?? error}`, 'error')
    }
  }

  async function tick() {
    if (stopped) return
    if (await probe(cfg.port, cfg.probeTimeoutMs)) {
      if (failures) log(ctx, `healthy again (was ${failures} consecutive failures)`)
      failures = 0
      return
    }
    failures += 1
    if (failures < cfg.failureThreshold) return

    // 探活失败。两种情形要分开处理：
    //   a) 端口没人听 → 服务根本没在跑，直接拉起；
    //   b) 端口有人听但不应答 → 僵尸进程（事件循环卡死）。它可能是我们拉的那个，
    //      也可能是旧 watchdog / 上次遗留的。不清理的话新进程 bind 会失败，就会死循环。
    //      清理前先核对命令行里确实是 image-shim.py，避免误杀别人占这个端口的进程。
    if (child) {
      try {
        child.kill()
        log(ctx, `killed unresponsive shim pid=${child.pid}`, 'warn')
      } catch {
        /* already gone */
      }
      child = null
    } else if (await portListening(cfg.port)) {
      const killed = killShimOnPort(cfg.port)
      log(ctx, killed ? `killed stale shim pid=${killed} holding port ${cfg.port}` : `port ${cfg.port} busy with a non-shim process — not touching it`, killed ? 'warn' : 'error')
    }

    launch(`unhealthy x${failures}`)
    failures = 0

    // 给启动留点时间，下一轮再判
    await new Promise((r) => setTimeout(r, cfg.startupGraceSec * 1000))
  }

  // 启动即检查：不等第一个 interval
  tick().catch((error) => log(ctx, `initial probe failed: ${error?.message ?? error}`, 'warn'))

  timer = setInterval(() => {
    tick().catch((error) => log(ctx, `probe loop error: ${error?.message ?? error}`, 'warn'))
  }, cfg.probeIntervalSec * 1000)
  if (timer.unref) timer.unref()

  if (ctx && typeof ctx.effect === 'function') {
    ctx.effect(
      () => () => {
        stopped = true
        if (timer) clearInterval(timer)
        timer = null
        if (cfg.stopOnDispose && child) {
          try {
            child.kill()
            log(ctx, `stopped shim pid=${child.pid} (stopOnDispose=true)`)
          } catch {
            /* already gone */
          }
          child = null
        }
      },
      'image-shim: probe loop',
    )
  }
}
