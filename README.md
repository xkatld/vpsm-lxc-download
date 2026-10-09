# vpsm-lxc-download

使用 **一个 GitHub Actions 工作流**，在 GitHub-hosted Linux runner 上原生构建 x86_64（`amd64`）和 ARM64（`arm64`）Incus/LXC 容器镜像。不需要自建 runner，不需要预装 Incus，也不需要配置构建密码 Secret。

> 旧双规格流水线已通过真实双架构构建；当前改为每个版本/架构只构建一份完整镜像，不再生成 lite，文件名也不含 all。此单规格改动仍需新的 Actions 实测。镜像身份初始化等验收问题尚未解决，不应将构建通过等同于生产模板验收通过。

## 镜像范围

| 发行版 | 版本 |
|---|---|
| AlmaLinux | 8、9、10 |
| Alpine | 3.21、3.22、3.23、3.24 |
| CentOS Stream | 9-Stream、10-Stream |
| Debian | bookworm、trixie、forky |
| Ubuntu | jammy、noble、resolute |

- 15 个版本 × 2 种架构 = **30 条基础镜像记录**，以 `镜像.md` 为准。
- 上游 variant 仅使用 `default`，并要求支持 Incus container。
- 每条记录只构建一份完整镜像，共 **30 个目标镜像**；不再区分 all/lite。
- 所有镜像均配置 SSH，并安装 `bash git unzip screen wget curl sudo nano ca-certificates`：保留原 8 个常用工具，显式补齐 HTTPS CA 证书包。
- 安装后刷新系统 CA 信任库并检查证书包非空，失败即中止；这不等于对任意外部站点的 HTTPS 连通性验收。
- CentOS Stream 不是传统 CentOS Linux；Debian Forky 是开发分支，不能将整张清单称为“全 LTS”。
- 本项目构建容器，不构建 Incus VM。x86 指 x86_64，不含 32 位 i386。

## 一个工作流，完成整个流程

唯一工作流：`.github/workflows/build-images.yml`。

```text
validate：单元测试、检查清单、生成矩阵
    ↓
build：30 个矩阵任务（最多同时执行 6 个）
    ├─ amd64：ubuntu-24.04
    └─ arm64：ubuntu-24.04-arm
    每个任务：
    自动安装 Incus → 初始化存储与 NAT/DHCP 网络
    → 下载一个基础镜像 → 启动一个构建容器
    → 配置 SSH → 安装软件 → 写入说明 → SSH 登录测试
    → 清理缓存与测试凭据 → 停止、发布为本地 Incus 镜像
    → 导出、生成 SHA256SUMS 和构建报告 → 清理临时资源
    → 上传 Actions Artifact
    ↓
verify：要求所有矩阵任务成功，逐个下载 Artifact 验证摘要和 SHA-256
```

每台 runner 只处理一个版本的一个架构，不会在一个磁盘中堆积全部镜像。汇总阶段也逐个下载、校验、删除本地副本；Actions 中的 Artifact 不受影响。

### 触发与下载

- **手动**：仓库 → Actions → Build Incus images → Run workflow。
- **主分支推送**：相关代码、清单、工作流变化时执行完整构建。
- **每周定时**：周一 02:17 UTC，构建清单内版本的最新上游镜像。
- **Pull Request**：同一工作流中只执行 `validate`，不执行特权镜像构建。

构建完成后，在该次运行页面的 Artifacts 下载，例如：

```text
images-alpine-3.24-amd64
images-alpine-3.24-arm64
```

每份 Artifact 包含一个目标镜像的导出文件、`SHA256SUMS`、`build-summary.json`。导出名称不含 `all` 或 `lite`，例如：

```text
alpine324-amd64-lxc.tar.gz
debian12-arm64-lxc.tar.gz
centos9-stream-amd64-lxc.tar.gz
```

Incus 可能导出统一文件或 metadata/rootfs 分离文件；一个镜像不一定对应一个物理文件，导入时必须保留配套文件。当前 Artifact 保留 **7 天**。旧运行的双规格产物不会被此修改重命名或删除；新校验器只接受单规格名称，不混用新旧产物。

旧双规格产物并非完全等大：例如 amd64 的 Alpine 3.24 约 18.1/9.5 MiB，Ubuntu noble 约 160.9/137.7 MiB（原 all/lite，来自运行 `37944528646`）。选择单规格是为了减少重复构建和维护；按该次实际导出大小计算，去掉 lite 可减少约 **44.5%** 的总产物字节数，但不能保证构建耗时等比例下降。

这里的“发布镜像”指 `incus publish`，**不是自动创建 GitHub Release**。当前完整流程终点是可下载并经过校验的 Actions Artifacts。

### 使用要求与费用

GitHub 仓库需要启用 Actions，并允许使用 `ubuntu-24.04` 和 `ubuntu-24.04-arm` runner、官方 checkout/upload/download actions。组织策略、并发额度和计费限制仍适用；私有仓库请关注 Actions 分钟数、Artifact 容量和额度。

无需添加密码 Secret：构建器在内存中自动生成随机测试密码，不使用环境变量 `IMAGE_ROOT_PASSWORD`，不将密码写入命令参数、报告或日志。`GITHUB_TOKEN` 由 GitHub 自动提供，只有汇总任务需要 `actions: read` 来下载本次产物，没有仓库写权限。

## 凭据与镜像使用

**导出前锁定 root 密码，测试用随机密码不会成为发布镜像的登录密码。** 原来的公共默认密码 `vpsm.link` 不再用于新流水线。导入镜像并启动容器后，请通过 Incus 控制台自行配置密码或 SSH 公钥，例如：

```bash
incus exec <容器名> -- passwd root
```

这意味着安装并测试过 SSH，但用户必须先配置自己的凭据才能从外部登录。不要将生产密码、SSH 私钥或云服务 token 注入镜像构建。

**当前限制：尚未实现首次启动时自动生成独立 SSH 主机密钥。** 导出镜像保留构建时的主机密钥，同一镜像创建的容器会共享服务器身份。每个新容器在接入不可信网络前，必须通过 Incus 控制台重新生成密钥并重启 SSH；这与设置 root 密码是两回事：

```bash
incus exec <容器名> -- sh -c 'rm -f /etc/ssh/ssh_host_* && ssh-keygen -A'
# Alpine
incus exec <容器名> -- rc-service sshd restart
# Debian / Ubuntu
incus exec <容器名> -- systemctl restart ssh
# AlmaLinux / CentOS Stream
incus exec <容器名> -- systemctl restart sshd
```

按发行版选择一条重启命令。在完成密钥更换和访问控制之前，不应将这些镜像视为可直接公网部署的加固模板。

## 本地使用

本地运行需 Python 3.10+、同架构 Linux、已经初始化的 Incus、可用的 `images:` remote、OpenSSH 客户端和 `sshpass`。`scripts/setup-incus.sh` 专用于**一次性的 GitHub-hosted runner**，不要在已有 Incus 生产宿主机上运行。

只生成矩阵，不启动 Incus、不需要密码：

```bash
python3 build_pipeline.py --manifest '镜像.md' --matrix
```

运行测试，不启动真实容器：

```bash
python3 -m unittest discover -s tests -v
```

本机为 x86_64 时构建一个版本：

```bash
python3 build_pipeline.py --manifest '镜像.md' \
  --architecture amd64 --distro alpine --release 3.24 --output-dir dist
```

ARM64 主机使用 `--architecture arm64`。使用拥有 Incus 管理权限的专用用户运行；输出目录须为空，以免将上次产物误报为本次结果。构建器不会在 x86_64 上模拟 ARM64。

## 失败与资源清理

### Incus 初始化时提示 images 源已存在

如果旧版工作流在 `Install and initialize Incus` 阶段报错：

```text
Error: Remote images exists as <https://images.linuxcontainers.org>
```

原因是旧脚本使用了不存在的 `incus remote get-url` 命令，把命令失败误判为源不存在，然后重复添加默认的 `images` 源。现改用 Incus 6.0 支持的 `incus remote list --format=json` 精确检查源名；查询失败时直接报错，不再尝试添加。

修复需要提交并推送后，通过新的 push 或手动触发构建来验证。不要只点旧运行的 **Re-run jobs**：它仍使用旧提交，不会包含修复。

### 容器启动后的软件源就绪

容器能够执行 `true` 不代表 DHCP、DNS 和软件源已就绪。流水线在安装 SSH 之前，以包管理器刷新索引作为就绪探测：Alpine 使用 `apk update`、Debian/Ubuntu 使用严格报错的 `apt-get update`、AlmaLinux/CentOS 使用 `dnf makecache`。探测有总截止时间和单次超时；超时仍失败，不跳过安装或 SSH 测试，也不无限重试。

SSH 安装配置拆分为具名步骤。工作流启用 `--setup-diagnostics`，仅对明确不含凭据的步骤保留限长、脱敏的 stderr；密码设置和 SSH 登录测试仍隐藏输出。本地可按需添加此选项，默认不披露子进程输出。失败步骤及安全错误信息会进入日志和 `build-summary.json`，便于区别网络未就绪、包安装失败和服务启动失败。

本地复现及对照实验见 [Incus 实测记录](docs/local-incus-reproduction-2026-10-09.md)。本地 x86_64 验证不等于远程 ARM64 构建成功，以对应 Actions 运行结果为准。

### 资源与诊断

- `fail-fast: false`：一个版本失败不会取消其他版本；最终汇总仍标记失败，不能假装全量成功。
- 正常结束、异常和可处理的终止信号会清理本次创建的资源；runner 被强杀时由 GitHub 销毁临时 VM 兜底。
- 构建使用唯一资源前缀，不再扫描和操作所有容器；不按共享基础镜像 fingerprint 删除其他 alias 指向的镜像。
- 失败任务尝试上传独立 `diagnostics-*` Artifact。只有完整成功的任务上传镜像产物。
- 清单中的 Build date 是来源快照日期，不是镜像版本锁定；实际下载使用上游可变 alias，不保证每次字节一致。

## 镜像验收

交付验收必须确认 **IPv4/IPv6 双栈**，两种原生架构均需覆盖。对实际导出镜像重新导入后，分别测试地址/路由、A/AAAA 解析、强制 IPv4/IPv6 的 SSH 与 HTTPS，以及重启后的持久性。完整标准见 [镜像交付验收要求](docs/image-acceptance.md)。

**当前双栈尚未验收：** 构建网桥设置为 `ipv6.address: none`，现有 SSH 测试仅使用 IPv4。runner 缺少公网 IPv6 时必须标记环境阻塞，不得将跳过视为通过，也不能用内网 IPv6 成功代替公网双栈结论。

## 文件结构

```text
.github/workflows/build-images.yml  # 唯一工作流
build_pipeline.py                   # 双架构统一构建入口
scripts/setup-incus.sh              # 临时 runner 的 Incus 初始化
scripts/verify_artifacts.py          # 汇总任务的产物校验
tests/                              # 无真实 Incus 副作用的单元测试
镜像.md                             # 30 条 amd64/arm64 default 记录
01_*.py ... 09_*.py                  # 历史分步脚本，不被 Actions 调用
```

**不要再用历史编号脚本执行新双架构清单**：它们保留了旧的 ARM64 假设和全局容器扫描行为，仅作为历史参考。当前维护入口为 `build_pipeline.py`。
