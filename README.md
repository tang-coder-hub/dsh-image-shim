# dsh-image-shim

把本地图像去水印代理（`image-shim.py`，默认 `127.0.0.1:3089`）挂在 DSH 宿主的生命周期上。
DSH 起来它就在，掉了自动拉回来 —— 不再依赖「登录时触发一次」的开机自启。

## 它解决什么

出图链路（`dsh-draw` 的 `cogview-clean` 引擎、我这边调 `image_generate`）都指向 `127.0.0.1:3089`。
这个端口上是 `image-shim.py`：取智谱 CogView 的图 → 抹掉右下角「AI生成」角标 → 转无损 PNG 返回。

原来靠 `HKCU\...\Run` 自启，两个坑：

1. **两条自启互相抢端口**（`image-shim-cogview-clean` 与 `image-shim-watchdog`）——
   先起的占住 3089，后起的默默死掉，于是「自启项都在、服务却没活」。
2. **只在登录时触发一次**。DSH 崩溃/重启、或服务中途退出，就再没人管它。

现在：插件在宿主 `apply` 时探活，不健康就拉；之后每 30 秒复查一次。

## 探活不是探 TCP

`GET /draw/status` 要求 **200 且 `ok === true`**。
只探 TCP 会把「进程还在、事件循环卡死」误判成健康 —— 那种情况下请求会一直挂着。

## 行为细节

| 行为 | 说明 |
|---|---|
| 启动即检查 | 不等第一个 interval，`apply` 里立刻探一次 |
| 连续失败才动手 | 连续 `failureThreshold`（默认 2）次失败才拉起，避免网络抖动误杀 |
| 僵尸清理 | 端口有人听但探活失败 → 杀掉**我们自己拉起的那个** child 再重拉 |
| 不误杀别人 | 只 kill 自己 spawn 的进程；端口被别人占着时只记日志 |
| 脱离宿主 | `spawn(..., detached: true)` + `unref()`，DSH 重启不带走它 |
| 优雅退出 | `ctx.effect` 清理定时器；`stopOnDispose: true` 时连进程一起停 |

## 配置

`cordis.patch.yml` 里这一段，全部有默认值，不配也能跑：

```yaml
- insert:
    - id: image-shim
      name: dsh-image-shim
      config:
        enabled: true
        pythonPath: auto
        port: 3089
        probeIntervalSec: 30
        probeTimeoutMs: 6000
        failureThreshold: 2
        startupGraceSec: 8
        stopOnDispose: false
```

- `pythonPath`：默认 `auto` —— 自动找解释器（详见下面「Python 解释器探测」）
- `port`：改了要和 `dsh-draw` 的引擎 `baseURL` 一起改
- `probeIntervalSec` / `probeTimeoutMs`：探活间隔与单次超时。**超时不要设太小** ——
  出图请求本身要 10 秒左右，机器满载时 4 秒超时可能误判成不健康，触发一次不必要的重启
- `stopOnDispose`：默认 `false` —— DSH 关掉后代理继续活着，下次启动直接复用

## 依赖

- `image-shim.py` / `dewatermark.py` 与本文件同目录（插件自带副本，不依赖外部路径）
- Python 侧需要 `Pillow`、`numpy`（探测时会真跑 `import PIL, numpy` 验证，缺一个就换下一个候选）
- 智谱 key 从 `~/.dsh/.credentials.yaml` 或 `GLM_API_KEY` 环境变量读

## Python 解释器探测

`pythonPath: auto`（默认）时按以下顺序逐个试，**第一个能 `import PIL, numpy` 的就用**：

1. 配置里写死的路径（`pythonPath` 不是 `auto` 时）
2. 环境变量 `DSH_IMAGE_SHIM_PYTHON`
3. `py -3` 启动器指向的解释器（系统注册的官方 Python）
4. `%LOCALAPPDATA%\Programs\Python\Python3*`
5. `C:\` `D:\` `E:\` 下的 `Python3*` 目录
6. `PATH` 里的 `pythonw.exe` / `python.exe`

找到 `python.exe` 会自动换成同目录的 `pythonw.exe`（不弹黑框）。全找不到时日志会打出所有试过的路径，方便定位。

注意：PATH 里常混进别的软件自带的 Python（FreeCAD、某些 IDE）。那些能用但不可靠 —— 卸载那个软件就断了，所以只当兜底，排在官方安装之后。

## 手动验证

```powershell
curl http://127.0.0.1:3089/draw/status
# {"ok":true,"shim":{"pid":...},"requests":0,...}
```

日志：`<插件目录>/draws/shim.log`。

## 装 / 卸

```powershell
# 从 GitHub 装（推荐）
dsh plugin --profile desktop add github:tang-coder-hub/dsh-image-shim

# 或从本地目录装（开发调试时用）
dsh plugin --profile desktop add 'file:<插件目录绝对路径>'

# 卸载
dsh plugin --profile desktop remove dsh-image-shim
```

host 侧插件在 DSH 启动时加载；但如果宿主已经跑着，`dsh plugin add` 之后 bundle 会被热挂载，实测不用重启也能生效（本机 2026-10-25 验证：装完直接杀 shim，DSH 主进程把它拉回来了）。
装好后建议删掉 `HKCU\...\Run` 里那条 `image-shim-watchdog` —— 两套守护会抢着拉，虽然不会坏（先起的占住端口、后起的探活成功就退出），但没必要。

## 使用边界

这个插件的用途是：把上游图像服务返回的图片里**由服务端叠加的水印角标**抹掉，然后转成无损 PNG 落盘。

它不破解任何付费墙、不绕过用量限制、不动 DRM —— 请求照常发给上游、照常计费。

但请注意：**抹掉生成内容上的 AI 标识，在部分服务商的条款里属于不允许的行为**（智谱 CogView 目前把「AI生成」角标直接烧在像素里，且请求参数 `watermark_enabled: false` 无效）。

所以：**仅用于本地个人工作流。使用前请自行确认你所调用的图像服务商的服务条款是否允许。** 由此产生的任何后果由使用者承担。

如果你需要的是「合规地去标识」，更稳的路子是换一家提供无水印输出、或在条款里明确允许关闭标识的服务商，而不是在客户端抹。

## 许可

MIT
