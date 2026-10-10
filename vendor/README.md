# vendor/

本目录只保存本地评估用的上游仓库克隆。默认 `.gitignore` 会忽略这些仓库，避免嵌套仓库和历史污染本仓库。

当前评估对象：

- `vendor/anydoc`（MIT）
- `vendor/markitdown`（MIT）
- `vendor/docling`（MIT）

规则：

1. 只 clone/评估，不 fork、不修改 vendor 内源码。
2. 不要删除或改写上游 LICENSE。
3. 升级 vendor 时记录上游 commit。
4. 闭源商用使用 Import 私有仓库策略，不公开发布 vendor 代码。
