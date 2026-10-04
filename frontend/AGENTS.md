# 前端维护约定

- 技术栈固定为 React + TypeScript + Vite + Ant Design，优先复用现有组件和主题变量。
- 模型调用、工具执行、密钥读取、任务状态判定在 Python 后端完成，浏览器只提交配置和展示结果。
- `src/api/schema.d.ts` 由 OpenAPI 生成，不手工修改。后端接口改动后，在项目根目录执行 `conda run --no-capture-output -n agent python scripts/export_openapi.py`，再在本目录执行 `npm run generate:api`。
- 新组件放在 `src/components/`；HTTP 调用统一使用 `src/api/client.ts`；订阅生命周期在 `src/hooks/`。避免在一个组件里混合接口、订阅和复杂展示逻辑。
- 停止请求成功表示 `stopping`，只有后端终态表示 `cancelled`。不能根据 SSE 断开或模型请求超时自行标记任务完成。
- 修改后执行 `npm run test` 与 `npm run build`。新交互应覆盖重要行为；适当时在浏览器中验证，工具不可用时明确报告未验证的范围。
- 使用 `npm ci` 安装锁文件中的依赖；升级依赖时先检查兼容范围，不能用 `--force` 或 `--legacy-peer-deps` 隐藏依赖冲突。
- 不将 `.env`、模型密钥、任务数据、截图编码或 node_modules 提交到版本控制。
