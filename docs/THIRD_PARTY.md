# 第三方组件与许可说明

## faceair/clash-speedtest

- 用途：执行节点下载测速；App 构建时会将测速组件放入应用包。
- 上游项目及源码：[faceair/clash-speedtest](https://github.com/faceair/clash-speedtest)，固定上游版本 [v1.8.6](https://github.com/faceair/clash-speedtest/tree/v1.8.6)，[发行页面](https://github.com/faceair/clash-speedtest/releases/tag/v1.8.6)。
- 上游许可证：[GNU GPL version 3.0（GPL-3.0）](https://github.com/faceair/clash-speedtest/blob/v1.8.6/LICENSE)。感谢上游作者及其依赖项目的贡献；各自的许可和版权声明以相应上游材料为准。
- 目标归档：[clash-speedtest_Darwin_arm64.tar.gz](https://github.com/faceair/clash-speedtest/releases/download/v1.8.6/clash-speedtest_Darwin_arm64.tar.gz)，SHA-256：`e41fa9b2bf7409206ed9e58e4dbc4b0b7b09d3f2d8a6946bfdad2b85a14d2fb2`。这是**归档**散列，不是解压后二进制的散列。构建脚本设计为从上述官方 release 下载并核对固定散列，失败则停止。

本源码仓库不附带上游二进制；默认构建的 App 会包含下载后的 worker、从校验过的归档提取的上游许可证和来源说明，并对 bundle 内的 worker 重新做本机临时签名。校验固定散列能检查下载内容是否与预期归档一致，但不是独立的来源真实性或安全性保证。上游发布构件带有 **dirty build** 标记，不能宣称该二进制可由 v1.8.6 tag 的干净源码逐字节复现；仅凭 tag 和 SHA-256 也不能证明完整的对应源码。分发包含该组件的构建物之前，应审阅上游许可、版权声明及相应源码提供等义务，必要时核实构建来源或联系上游；此说明不构成法律意见，也不保证分发已合规。

## 本项目

本项目自身的许可证尚未指定。上游 GPL-3.0 适用于相应上游组件，不能仅凭此自动宣称它是本项目的许可证。公开查看不自动授予一般性的使用、修改和再分发权限；GitHub 服务条款可能允许查看和 fork。若希望他人进一步使用或再分发，请由权利人明确本项目许可，并在分发构建物前复核全部适用的第三方义务。
