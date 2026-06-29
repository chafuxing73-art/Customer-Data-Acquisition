# CI/CD 工作流说明

## 概述

本项目使用 GitHub Actions 实现了完整的 CI/CD 流水线，包括代码检出、依赖安装、应用构建、单元测试、Docker 镜像构建和推送。

## 工作流配置

工作流文件位于 `.github/workflows/ci-cd.yml`，包含以下两个主要任务：

1. **build-test**：执行代码检出、依赖安装、测试和应用构建
2. **docker-build**：构建 Docker 镜像并可选地推送至 Docker Hub

## 环境变量和 Secrets

### 必需的 Secrets

要启用 Docker 镜像推送功能，需要在 GitHub 仓库中设置以下 Secrets：

- `DOCKER_USERNAME`：Docker Hub 用户名
- `DOCKER_PASSWORD`：Docker Hub 访问令牌（推荐使用访问令牌而非密码）

### 可选的环境变量

应用运行时可通过环境变量配置：

- `ONETOUCH_REQUEST_CAPTURE_FILE`：请求抓包文件路径
- `ONETOUCH_RESPONSE_FIXTURE_FILE`：响应样例文件路径
- `WEDOC_WEBHOOK_URL_GANGQIAN`：港前文档 webhook 地址
- `WEDOC_WEBHOOK_URL_GANGHOU`：港后文档 webhook 地址

## 工作流触发条件

工作流会在以下情况下触发：

- 推送到 `main` 或 `master` 分支
- 对 `main` 或 `master` 分支的 Pull Request
- 手动触发（通过 GitHub 界面）

## Docker 镜像

构建的 Docker 镜像标签格式：
- 基于提交 SHA：`{用户名}/customer-data-fetch:{commit-sha}`
- 最新版本：`{用户名}/customer-data-fetch:latest`

## 本地测试

### 构建 Docker 镜像

```bash
docker build -t customer-data-fetch .
```

### 运行容器

```bash
docker run -p 5000:5000 --env-file .env customer-data-fetch
```

其中 `.env` 文件包含所需的环境变量配置。

## 注意事项

1. 本工作流使用了缓存机制来加速构建过程
2. 测试步骤目前只是占位符，需要根据实际项目添加测试用例
3. Docker 镜像推送功能仅在提供了 Docker Hub 凭据时启用
4. 工作流使用了最新版本的 GitHub Actions 官方动作，确保了安全性和稳定性