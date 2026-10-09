# 本地 Incus 实测记录：Alpine SSH 配置阶段失败

日期：2026-10-09。构建实测时间：14:04–14:11 UTC。

## 结论

这次执行了真实镜像下载、容器启动和完整构建，不是 mock 测试。

1. 使用原仓库流水线的业务步骤，Alpine **3.21 / 3.24 amd64** 均复现了第 3 步失败，底层为 `apk update` 的临时软件源访问错误；3.24 明确报告 `DNS: transient error`。
2. 容器可执行 `true` 时，网络/DNS/软件源并不一定已就绪。等待真实 `apk update` 成功后，Alpine **3.21、3.22、3.23、3.24 amd64** 的 all/lite 全流程全部成功。
3. 共导出 **8 个镜像**，真实 SSH 密码登录测试全部通过；项目产物验证器检查摘要、文件集合及 SHA-256 全部通过；直接读取每个导出归档确认 root 的 shadow 字段精确为 `!`。
4. 这是本地已证实的启动就绪时序问题，与 CI 中 Alpine 的阶段/退出码相符，但旧 CI 未保留详细错误，不能声称其所有失败已被逐项证实。**ARM64 的 AlmaLinux 10 / CentOS Stream 10 未在本机复现，仍待原生 ARM64 验证。**
5. 本次只做实验与记录，未把实验改动合入 `build_pipeline.py`，未提交、推送或触发 GitHub Actions。报告中的成功不能等同于远程 CI 已修复。

## 环境与隔离

- 仓库基线：`f753dfe`。
- 本机：Linux x86_64，Incus 客户端/服务端均为 7.5.1；CI 使用 Ubuntu 24.04 的 Incus 6.0，环境并不完全相同。
- 原有 `default` ZFS 存储池状态为 `Unavailable`；没有尝试修复、格式化或修改它。
- 原有 `vpsmbr0`、默认 profile、宿主机代理/策略路由均保留。
- 创建专用项目 `vpsm-repro-20261009`、同名 `dir` 存储池及 NAT 网桥 `vpsm-test0`。
- 网桥地址：`10.203.109.1/24`，已检查现有直接连接网段不重叠。没有关闭宿主机防火墙。
- 本机有 mihomo TUN / DNS 代理。容器 DNS 查询曾返回 `198.18.0.20`，因此本地网络路径不同于 GitHub runner。
- 为执行原流水线的真实 SSH 测试，安装了原来缺少的 `sshpass`（Debian 1.10-0.1）；测试结束后保留此小型依赖。
- 测试用随机 root 密码只通过 stdin/受控子进程环境传递，没有记录到日志。

## 1. 初始化隔离环境

执行了以下等价的成功命令序列（名称仅适用于本次；重复实验应换新名称，并先检查网络地址冲突）：

```bash
incus project create vpsm-repro-20261009 \
  -c features.networks=false -c features.images=true -c features.profiles=true
incus storage create vpsm-repro-20261009 dir
incus network create vpsm-test0 \
  ipv4.address=10.203.109.1/24 ipv4.nat=true ipv6.address=none
incus --project vpsm-repro-20261009 profile device add default root disk \
  path=/ pool=vpsm-repro-20261009
incus --project vpsm-repro-20261009 profile device add default eth0 nic \
  network=vpsm-test0 name=eth0
incus image copy images:alpine/3.24/amd64 local: \
  --target-project vpsm-repro-20261009 --alias alpine-324-repro --quiet
incus --project vpsm-repro-20261009 launch alpine-324-repro alpine324
```

准备过程中遇到并处理的本地问题：

- 网桥名最长 15 个字符；最初使用项目名作为网桥名被拒绝，改用 `vpsm-test0`。
- `ipv4.address=auto` 因现有路由无法自动选出空闲网段，改为上述人工核对的网段。
- `image copy` 的目标项目必须显式指定 `--target-project`，仅设置全局 `--project` 不能保证跨 remote 的目标项目。首次复制到 default 项目的测试镜像已删除。
- 本地项目间直接 image copy 提示源服务器未监听网络；没有为此开放 Incus API，改为直接从上游复制到指定目标项目。

## 2. 手工观察启动时序

容器刚启动后立即执行：

```bash
incus --project vpsm-repro-20261009 exec alpine324 -- sh -c \
  'cat /etc/alpine-release; apk update'
```

实际输出：

```text
3.24.2
WARNING: updating and opening https://dl-cdn.alpinelinux.org/alpine/v3.24/main/x86_64/APKINDEX.tar.gz: DNS: transient error (try again later)
WARNING: updating and opening https://dl-cdn.alpinelinux.org/alpine/v3.24/community/x86_64/APKINDEX.tar.gz: DNS: transient error (try again later)
2 unavailable, 0 stale; 34 distinct packages available
```

退出码为 **2**。随后再次检查时，容器已取得 `10.203.109.83/24` 地址、默认路由和 DHCP 下发的 DNS `10.203.109.1`；`nslookup` 和 `apk update` 均成功。没有改软件源、禁用证书验证或调整 SSH 包名。

随后执行原有的 `apk add openssh`、`ssh-keygen -A`、SSH 配置、`rc-update add sshd` 和 `rc-service sshd restart`，全部成功。实际安装包含 `openssh-server-common-openrc`，没有证据支持“缺 OpenRC 包”这一猜测。

## 3. 原流程与就绪实验对照

实验入口 `reproduce.py` 导入仓库的 `Pipeline`，只通过子类做如下适配：

- 所有 Incus 调用限制到专用项目；下载显式添加 `--target-project`。
- 对不含凭据的 APK/SSH 安装命令失败，输出经过密码替换且逐行加前缀的 stderr，以读取真实原因。
- **基线组**保持原来的 `wait_for_container`，只检测 `incus exec ... true`。
- **实验组**在原等待后，增加最多 120 秒的 `apk update` 软件源就绪探测；单次最多 30 秒，失败间隔最多 2 秒；超时仍失败。不重试整个构建，不跳过 SSH，不吞掉永久错误。
- 密码设置、all/lite 区分、SSH 测试、凭据清理、发布、导出和摘要生成全部调用原实现。
- 实验 `build_date` 明确记为 `local-reproduction`；真实上游标识单独记录如下。

运行命令：

```bash
python3 -u reproduce.py --release 3.24 --output baseline-324
python3 -u reproduce.py --release 3.24 --ready --output ready-324
python3 -u reproduce.py --release 3.21 --output baseline-321
python3 -u reproduce.py --release 3.21 --ready --output ready-321
python3 -u reproduce.py --release 3.22 --ready --output ready-322
python3 -u reproduce.py --release 3.23 --ready --output ready-323
```

注意：适配器中的仓库路径、项目名为本机实验值，重跑前应确认；输出目录必须不存在或为空。

| 版本/架构 | 原流程对照 | 增加软件源就绪探测 | SSH / 导出 / 校验 |
|---|---|---|---|
| Alpine 3.21 amd64 | 第 3 步失败，APK 临时错误，exit 2 | 成功 | all/lite 均通过 |
| Alpine 3.22 amd64 | 未额外运行基线组 | 成功 | all/lite 均通过 |
| Alpine 3.23 amd64 | 未额外运行基线组 | 成功 | all/lite 均通过 |
| Alpine 3.24 amd64 | 第 3 步失败，DNS transient error，exit 2 | 成功 | all/lite 均通过 |

实验组四个版本的两个容器均在第 **2 次**就绪探测成功。

成功组时间（UTC）：

- 3.24：14:05:13–14:06:34。
- 3.21：14:07:16–14:08:28。
- 3.22：14:08:39–14:10:05。
- 3.23：14:10:05–14:11:22。

## 4. 下载的基础镜像身份

四个版本均为 `default` / amd64 / Incus container，上游 serial 为 `20261006_13:00`。

| 版本 | 基础镜像 fingerprint |
|---|---|
| 3.21 | `04f393e6720b218f2d275d6cb3aef063e929accba24335716dc5931aa968d59e` |
| 3.22 | `bb6a7bdd503422977c9fca86fb25618b7f406c40b7a1105747360f499118ae00` |
| 3.23 | `420645a7d5bafdb1a3e155938ecccea3aa4bca2acb1df9ed8a67298d53f7b522` |
| 3.24 | `ec12d249413041d2db64856872256194547bba9f2e870f729dbb4d93fb5a6c1e` |

## 5. 全流程验证

每次成功组实际完成：下载 → 创建两个容器 → 软件源就绪 → 安装配置 SSH → 为 all 安装常用软件 → 写说明 → sshpass 真实登录并检查 `whoami=root` → 锁 root、清缓存 → 停止/发布 → tar.gz 导出 → SHA256SUMS/摘要 → 清理本次容器及 alias。

使用仓库 `scripts.verify_artifacts.verify_artifact` 对四份产物执行校验，全部通过。另用 Python `tarfile` 只读归档成员、不落盘解包，确认八个镜像 `/etc/shadow` 的 root 字段均精确为 `!`。

| 文件 | 字节数 |
|---|---:|
| alpine321-all-amd64-lxc.tar.gz | 17,673,902 |
| alpine321-lite-amd64-lxc.tar.gz | 9,099,584 |
| alpine322-all-amd64-lxc.tar.gz | 18,506,979 |
| alpine322-lite-amd64-lxc.tar.gz | 9,778,120 |
| alpine323-all-amd64-lxc.tar.gz | 19,056,077 |
| alpine323-lite-amd64-lxc.tar.gz | 10,170,561 |
| alpine324-all-amd64-lxc.tar.gz | 18,951,763 |
| alpine324-lite-amd64-lxc.tar.gz | 9,960,909 |

这些实验镜像仍保留原流程的共享 SSH 主机密钥限制，不是经过公网安全加固的发行成品，也没有执行“导出后重新导入启动”测试。

## 6. 清理与证据

- 已删除手工容器、实验项目内的基础/发布镜像、实验 profile 设备、实验项目、专用网桥和专用 dir 存储池。
- 镜像清理仅限这次新建的独占项目，未遍历删除 default 项目原有内容。
- 清理后 default 项目实例与镜像均为空（与实验前一致）；原有 ZFS 池仍是原状态 `Unavailable`；原有网桥/profile 保持不变。
- 只保留 `sshpass` 安装、实验记录及导出文件；导出文件保存在仓库外，没有加入 Git。
- 完整本机证据目录：`/root/vpsm-local-repro-20261009`。包括实验适配器、6 份运行日志、摘要、校验文件、校验脚本及 8 个镜像。
- 提供精简证据包：报告、适配器、校验脚本、运行日志、摘要、SHA256SUMS、镜像身份元数据；不打包大型镜像，也不含测试密码。

## 后续修复依据

可以据此为流水线加入有截止时间的软件源/网络就绪处理及安全的失败步骤诊断；不要用固定长 sleep，也不要将所有包管理器错误无条件无限重试。正式修改仍须新增回归测试并重新运行原生双架构 CI。EL10 ARM64 失败不应未经验证就归因到同一问题。
