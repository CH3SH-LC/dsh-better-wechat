# 第三方归属与依赖

本项目原创插件、索引、编排、阅读器、文档和发布工具采用根目录 MIT 许可证。`collector/extract_local.py` 的加密格式、配置锚点与解码方法参考并改写自 [fanyuantaier/wechatauto-replica 的数据库模块](https://github.com/fanyuantaier/wechatauto-replica/blob/62503d76a0db069171cb4e42e87090ad285db489/wechatauto/db.py)。该文件以 Apache-2.0 发布，保留上游归属，并包含本项目加入的只读进程身份验证、密钥不落盘、WAL/SHM 校验和完整性失败处理等修改。

上游 [Apache-2.0 许可证](https://github.com/fanyuantaier/wechatauto-replica/blob/62503d76a0db069171cb4e42e87090ad285db489/LICENSE) 的原文副本位于 `LICENSES/Apache-2.0.txt`；该次核对的上游没有单独 NOTICE 文件。引用版本用于归属核对，不代表首次开发必然使用了同一个提交。未打包上游完整库，也未引入其发送消息或密钥缓存能力。

| 组件 | 使用方式 | 授权 / 来源 |
| --- | --- | --- |
| cryptography 48.0.0 | 用户另行安装，AES 等加密原语 | Apache-2.0 OR BSD-3-Clause；[官方项目](https://github.com/pyca/cryptography) |
| zstandard 0.25.0 | 用户另行安装，消息解压 | BSD-3-Clause；[官方项目](https://github.com/indygreg/python-zstandard) |
| SQLite | Python 标准库提供；参考 WAL 格式说明 | [公共领域说明](https://www.sqlite.org/copyright.html)、[WAL 格式](https://www.sqlite.org/walformat.html) |
| DSH / Schemastery | 由用户安装的 DSH 提供宿主 API | 本包不分发宿主实现，遵循相应上游许可 |
| GitHub Actions 官方动作 | 仅持续集成使用，未将实现嵌入包中 | 固定提交见 `tools/upstream-sources.json` |

依赖包安装后的许可证仍属于各依赖，不因本项目 MIT 声明而改变。微信与 DeepSeek Harness 的名称仅用于标识兼容对象，本项目不是其官方产品。
