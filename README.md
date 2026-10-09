# 容器镜像构建

使用一个 GitHub Actions 工作流，原生构建 amd64、arm64 的 Incus 容器镜像，无需自建运行器。

## 镜像范围

| 发行版 | 版本 |
|---|---|
| AlmaLinux | 8、9、10 |
| Alpine | 3.21、3.22、3.23、3.24 |
| CentOS Stream | 9-Stream、10-Stream |
| Debian | bookworm、trixie、forky |
| Ubuntu | jammy、noble、resolute |

以 [镜像清单](镜像.md) 为准，15 个版本、两种架构，共 30 个单规格镜像。上游使用 default，不构建虚拟机或 lite。CentOS Stream 并非传统 CentOS，Debian forky 是开发分支。

预装 SSH，以及 `bash git unzip screen wget curl sudo nano ca-certificates`，镜像说明地址为 `vpsm91.com`。

## 构建与下载

唯一工作流：`.github/workflows/build-images.yml`。

- 支持手动运行、相关主分支推送和每周一协调世界时 02:17 定时构建。
- 合并请求仅执行代码检查，不构建镜像。
- 每个任务完成构建、清理、导出、重导入及验收，通过后才上传镜像。
- 在运行页面下载产物，保留 7 天，不自动创建发行版发布。
- 镜像示例：`alpine324-amd64-lxc.tar.gz`，附带 `SHA256SUMS` 和 `build-summary.json`。
- 分离导出的元数据与根文件系统必须配套保留。
- 验收报告为 `acceptance-<发行版>-<版本>-<架构>`，失败诊断为 `diagnostics-*`。

## 使用与安全

发布前锁定 root 密码，无公共默认密码。启动后通过控制台设置自己的凭据：

```sh
incus exec <容器名> -- passwd root
```

新流程清理账户备份、测试状态和临时文件；导出前移除 SSH 主机密钥，停止后清空 machine-id。首次启动生成独立身份，重启保持稳定。

双栈验收以内网 IPv4、IPv6 均能 SSH 登录及重启后复测为准，不要求公网 IPv6。详见 [验收要求](docs/image-acceptance.md)。

[注意] 提交 `6d0e574` 已通过构建校验；新增身份初始化、残留清理及双克隆验收仍需新提交的远程实测。旧产物不会自动修复，功能验收不等于公网安全加固。上游镜像未固定指纹，不保证重复构建完全一致。

## 维护入口

| 文件 | 用途 |
|---|---|
| `build_pipeline.py` | 统一构建入口 |
| `scripts/setup-incus.sh` | 一次性运行器初始化，不用于已有生产宿主机 |
| `scripts/accept_image.py` | 重导入、双栈、重启及克隆身份验收 |
| `scripts/verify_artifacts.py` | 汇总产物校验 |
| `tests/` | 模拟单元测试 |
| `镜像.md` | 双架构构建清单 |
